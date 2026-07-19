from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import (  # noqa: E402
    audible_source,
    core,
    settings,
    shared,
)
from audible_goodreads_deal_scout import audible_fetch  # noqa: E402
from helpers import (  # noqa: E402
    AUDIBLE_HTML,
    fake_fetcher,
    row,
    write_rows,
)


class PrepareWorkflowTests(unittest.TestCase):
    def test_prepare_retries_transient_no_active_promotion(self) -> None:
        no_deal_html = AUDIBLE_HTML.replace(
            "Get today's Daily Deal before time runs out! $4.99 Deal ends @ 11:59PM PT.",
            "No active deal is visible yet.",
        )
        calls = {"count": 0}

        def flaky_fetcher(_: str) -> tuple[str, str]:
            calls["count"] += 1
            if calls["count"] == 1:
                return no_deal_html, "https://www.audible.com/dailydeal"
            return AUDIBLE_HTML, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"

        result = core.prepare_run(
            {"audibleMarketplace": "us", "audibleFetchRetries": 1, "audibleFetchBackoffSeconds": 0},
            fetcher=flaky_fetcher,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(calls["count"], 2)
        self.assertTrue(any("Retrying Audible daily promotion fetch" in warning for warning in result["warnings"]))

    def test_prepare_error_overwrites_current_prepare_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            artifact_dir = tmp / "artifacts"
            stale_path = artifact_dir / "prepare-result.json"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "reasonCode": "ready_public",
                        "metadata": {"storeLocalDate": "2026-04-27"},
                    }
                ),
                encoding="utf-8",
            )

            def failing_fetcher(_: str) -> tuple[str, str]:
                raise audible_fetch.AudibleFetchError("503 Service Unavailable")

            result = core.prepare_run(
                {
                    "artifactDir": str(artifact_dir),
                    "audibleMarketplace": "us",
                    "invocationMode": "scheduled",
                    "today": "2026-04-29",
                    "audibleFetchRetries": 0,
                },
                fetcher=failing_fetcher,
            )
            artifact_payload = json.loads(stale_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reasonCode"], "error_audible_fetch_failed")
        self.assertEqual(artifact_payload["reasonCode"], "error_audible_fetch_failed")
        self.assertEqual(artifact_payload["metadata"]["storeLocalDate"], "2026-04-29")

    def test_prepare_clears_stale_downstream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifact_dir = Path(tmp_dir) / "artifacts"
            artifact_dir.mkdir(parents=True)
            stale_names = [
                "runtime-output.json",
                "run-and-deliver-result.json",
                "mark-emitted-result.json",
            ]
            for name in stale_names:
                (artifact_dir / name).write_text(json.dumps({"stale": True}), encoding="utf-8")

            result = core.prepare_run(
                {
                    "artifactDir": str(artifact_dir),
                    "audibleMarketplace": "us",
                    "audibleFetchRetries": 0,
                },
                fetcher=fake_fetcher,
            )

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["metadata"]["clearedDownstreamArtifacts"], stale_names)
            for name in stale_names:
                self.assertFalse((artifact_dir / name).exists())

    def test_prepare_suppression_overwrites_current_prepare_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            artifact_dir = tmp / "artifacts"
            stale_path = artifact_dir / "prepare-result.json"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text(json.dumps({"status": "ready", "reasonCode": "ready_public"}), encoding="utf-8")
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="read")])
            result = core.prepare_run(
                {
                    "artifactDir": str(artifact_dir),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                    "today": "2026-04-29",
                },
                fetcher=fake_fetcher,
            )
            artifact_payload = json.loads(stale_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "suppress")
        self.assertEqual(artifact_payload["reasonCode"], "suppress_already_read")
        self.assertEqual(artifact_payload["metadata"]["storeLocalDate"], "2026-04-29")

    def test_prepare_public_mode_creates_no_personal_fit_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                },
                fetcher=fake_fetcher,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["reasonCode"], "ready_public")
            self.assertFalse(result["personalData"]["allowModelPersonalization"])
            self.assertEqual(result["personalData"]["csv"]["ratedOrReviewedCount"], 0)
            self.assertTrue(Path(result["artifacts"]["audiblePath"]).exists())

    def test_prepare_notes_mode_uses_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "notesText": "I like ambitious speculative fiction and locked-room mysteries.",
                },
                fetcher=fake_fetcher,
            )
            self.assertTrue(Path(result["artifacts"]["notesPath"]).exists())
        self.assertEqual(result["reasonCode"], "ready_notes")
        self.assertTrue(result["personalData"]["allowModelPersonalization"])

    def test_prepare_full_mode_includes_all_rated_or_reviewed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(
                export_path,
                [
                    row(title="Old Favorite", author="A Writer", shelf="read", rating="5"),
                    row(title="Review Only", author="B Writer", shelf="to-read", review="Wanted to remember this."),
                    row(title="Unrated", author="C Writer", shelf="read"),
                ],
            )
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
            self.assertTrue(Path(result["artifacts"]["runtimeInputPath"]).exists())
            self.assertTrue(Path(result["artifacts"]["runtimePromptPath"]).exists())
            self.assertTrue(Path(result["artifacts"]["runtimeOutputSchemaPath"]).exists())
            self.assertTrue(Path(result["artifacts"]["prepareResultPath"]).exists())
            self.assertTrue(Path(result["artifacts"]["fitContextPath"]).exists())
            self.assertTrue(Path(result["artifacts"]["reviewSourcePath"]).exists())
            prompt_text = Path(result["artifacts"]["runtimePromptPath"]).read_text(encoding="utf-8")
        self.assertEqual(result["reasonCode"], "ready_full")
        self.assertEqual(result["personalData"]["csv"]["ratedOrReviewedCount"], 2)
        self.assertEqual(result["personalData"]["csv"]["fitContextEntryCount"], 2)
        self.assertIn("summarize each review-bearing entry to 500 characters or fewer", prompt_text)
        self.assertIn("2 or 3 short sentences", prompt_text)

    def test_prepare_full_mode_writes_compact_fit_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(
                export_path,
                [
                    row(title="Reviewed", author="A Writer", shelf="read", rating="5", review="A" * 600),
                    row(title="Rated", author="B Writer", shelf="read", rating="4"),
                ],
            )
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
            fit_context = json.loads(Path(result["artifacts"]["fitContextPath"]).read_text(encoding="utf-8"))
            review_source = json.loads(Path(result["artifacts"]["reviewSourcePath"]).read_text(encoding="utf-8"))
        self.assertEqual(fit_context["entryCount"], 2)
        self.assertEqual(len(fit_context["entries"]), 2)
        self.assertNotIn("review", fit_context["entries"][0])
        self.assertEqual(review_source["entryCount"], 1)
        self.assertIn("reviewText", review_source["entries"][0])
        self.assertGreater(result["personalData"]["csv"]["contextBudget"]["savingsChars"], 0)

    def test_prepare_suppresses_exact_read_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="read", rating="5")])
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "suppress")
        self.assertEqual(result["reasonCode"], "suppress_already_read")

    def test_prepare_suppresses_exact_currently_reading_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="currently-reading")])
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "suppress")
        self.assertEqual(result["reasonCode"], "suppress_currently_reading")

    def test_prepare_to_read_match_stays_ready_and_overrides_threshold_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="to-read")])
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["personalData"]["exactShelfMatch"], "to-read")

    def test_prepare_ambiguous_personal_match_requires_conflicting_strong_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(
                export_path,
                [
                    row(title="Signal Fire", author="Jane Story", shelf="read"),
                    row(title="Signal Fire", author="Jane Story", shelf="to-read"),
                ],
            )
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reasonCode"], "error_ambiguous_personal_match")

    def test_duplicate_scheduled_run_suppresses_but_manual_run_ignores_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state_file = tmp / "state.json"
            first = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts-first"),
                    "audibleMarketplace": "us",
                    "stateFile": str(state_file),
                    "invocationMode": "manual",
                    "today": "2026-04-20",
                },
                fetcher=fake_fetcher,
            )
            core.mark_emitted(state_file, first["metadata"]["dealKey"])
            scheduled = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts-scheduled"),
                    "audibleMarketplace": "us",
                    "stateFile": str(state_file),
                    "invocationMode": "scheduled",
                    "today": "2026-04-20",
                },
                fetcher=fake_fetcher,
            )
            manual = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts-manual"),
                    "audibleMarketplace": "us",
                    "stateFile": str(state_file),
                    "invocationMode": "manual",
                    "today": "2026-04-20",
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(scheduled["reasonCode"], "suppress_duplicate_scheduled_run")
        self.assertEqual(manual["status"], "ready")

    def test_stale_warning_is_rate_limited_for_scheduled_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            state_file = tmp / "state.json"
            write_rows(export_path, [row(title="Old Favorite", author="A Writer", shelf="read", rating="5")])
            core.save_state(state_file, {"lastStaleWarningDate": "2026-04-18"})
            old_mtime = 1700000000
            export_path.touch()
            os.utime(export_path, (old_mtime, old_mtime))
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                    "stateFile": str(state_file),
                    "invocationMode": "scheduled",
                    "today": "2026-04-20",
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["warnings"], [])

    def test_privacy_mode_minimal_blocks_personal_data_from_model_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(
                export_path,
                [row(title="Old Favorite", author="A Writer", shelf="read", rating="5", review="Loved the ideas...!!")],
            )
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                    "notesText": "I like cerebral mysteries.",
                    "privacyMode": "minimal",
                },
                fetcher=fake_fetcher,
            )
            self.assertFalse(result["personalData"]["allowModelPersonalization"])
            self.assertNotIn("fitContextPath", result["artifacts"])
            self.assertNotIn("reviewSourcePath", result["artifacts"])
            self.assertNotIn("notesPath", result["artifacts"])
            runtime_input = json.loads(Path(result["artifacts"]["runtimeInputPath"]).read_text(encoding="utf-8"))
            prompt_text = Path(result["artifacts"]["runtimePromptPath"]).read_text(encoding="utf-8")
            self.assertEqual(runtime_input["personalDataSummary"]["fitContextApproxTokens"], 0)
            self.assertFalse(runtime_input["personalDataSummary"]["notesPresent"])
            self.assertIn("No personal CSV or notes artifacts are provided for this run", prompt_text)

    def test_prepare_returns_explicit_error_for_missing_notes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "notesFile": str(tmp / "missing-notes.md"),
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reasonCode"], "error_missing_notes_file")
        self.assertIn("Preference notes file not found", result["message"])

    def test_prepare_rejects_missing_csv_override_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="read", rating="5")])
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                    "csvColumnOverrides": {"title": "Wrong Header"},
                },
                fetcher=fake_fetcher,
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reasonCode"], "error_csv_unreadable")
        self.assertIn("references missing header 'Wrong Header'", result["message"])

    def test_shared_text_helpers_normalize_expected_values(self) -> None:
        self.assertEqual(shared.approx_token_count(""), 0)
        self.assertEqual(shared.normalize_review_text("<p>Wait...!!</p>"), "Wait.")

    def test_show_csv_headers_returns_detected_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story")])
            payload = core.show_csv_headers(export_path)
        self.assertEqual(payload["headers"][0], "Book Id")

    def test_measure_context_reports_savings_and_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            output_path = tmp / "fit-context.json"
            write_rows(
                export_path,
                [
                    row(title="Reviewed", author="A Writer", shelf="read", rating="5", review="B" * 500),
                    row(title="Rated", author="B Writer", shelf="read", rating="4"),
                ],
            )
            payload = core.measure_context(export_path, notes_text="I like cerebral fiction.", output_path=output_path)
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.with_name(output_path.stem + ".review-source.json").exists())
            self.assertEqual(payload["ratedOrReviewedRows"], 2)
            self.assertGreater(payload["contextBudget"]["legacyApproxTokens"], payload["contextBudget"]["fitContextBaseApproxTokens"])

    def test_parse_audible_chip_genres_filters_boilerplate_blob(self) -> None:
        html = """
        <adbl-chip>Literature &amp; Fiction</adbl-chip>
        <adbl-chip>Thought-Provoking</adbl-chip>
        <adbl-chip>English Espa\u00f1ol US Dollar Sign in Daily Deal $1.99 {"rating":{"count":19806}} Copy Link Audible Studios</adbl-chip>
        <adbl-chip>Fiction</adbl-chip>
        """
        self.assertEqual(
            audible_source.parse_audible_chip_genres(html),
            ["Literature & Fiction", "Thought-Provoking", "Fiction"],
        )

    def test_supported_marketplaces_include_non_us_release_target(self) -> None:
        self.assertIn("us", settings.SUPPORTED_MARKETPLACES)
        self.assertGreaterEqual(len([key for key in settings.SUPPORTED_MARKETPLACES if key != "us"]), 1)
