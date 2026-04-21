from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audible_goodreads_deal_scout import core, public_cli  # noqa: E402
from audible_goodreads_deal_scout import delivery as delivery_mod  # noqa: E402
from audible_goodreads_deal_scout import repo_audit  # noqa: E402
from audible_goodreads_deal_scout import rendering  # noqa: E402


GOODREADS_HEADERS = [
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
]


AUDIBLE_HTML = """
<html>
  <head>
    <script type="application/ld+json">
    {
      "@context":"http://schema.org",
      "@type":"Product",
      "productID":"ABC1234567",
      "name":"Signal Fire",
      "image":"https://example.com/cover.jpg",
      "offers":{"price":"14.95","priceCurrency":"USD"}
    }
    </script>
  </head>
  <body>
    <div>Get today's Daily Deal before time runs out! $4.99 Deal ends @ 11:59PM PT.</div>
    <adbl-product-metadata>
      <script type="application/json">{"authors":[{"name":"Jane Story"}]}</script>
    </adbl-product-metadata>
    <adbl-product-metadata>
      <script type="application/json">{"duration":"11 hrs and 48 mins","releaseDate":"07-12-22","categories":[{"name":"Science Fiction"},{"name":"Thriller"}]}</script>
    </adbl-product-metadata>
    <adbl-text-block slot="summary">A smart thriller with a clear Audible summary. It stays readable.</adbl-text-block>
  </body>
</html>
"""


def fake_fetcher(_: str) -> tuple[str, str]:
    return AUDIBLE_HTML, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GOODREADS_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def row(
    *,
    title: str,
    author: str,
    shelf: str = "",
    rating: str = "0",
    review: str = "",
    isbn: str = "",
    isbn13: str = "",
    bookshelves: str = "",
) -> dict[str, str]:
    return {
        "Book Id": "1",
        "Title": title,
        "Author": author,
        "Author l-f": "",
        "Additional Authors": "",
        "ISBN": isbn,
        "ISBN13": isbn13,
        "My Rating": rating,
        "Average Rating": "4.20",
        "Publisher": "",
        "Binding": "",
        "Number of Pages": "",
        "Year Published": "2022",
        "Original Publication Year": "2022",
        "Date Read": "",
        "Date Added": "2026-04-01",
        "Bookshelves": bookshelves,
        "Bookshelves with positions": "",
        "Exclusive Shelf": shelf,
        "My Review": review,
        "Spoiler": "",
        "Private Notes": "",
        "Read Count": "1",
        "Owned Copies": "0",
    }


def read_message_fixture(name: str) -> str:
    return (Path(__file__).resolve().parent / "fixtures" / "messages" / name).read_text(encoding="utf-8").rstrip("\n")


class AudibleGoodreadsDealScoutTests(unittest.TestCase):
    def test_setup_writes_config_and_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            result = core.setup_configuration(
                {
                    "storageDir": str(tmp),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": "/tmp/export.csv",
                    "notesText": "I like literary mysteries.",
                    "dailyAutomation": True,
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1000000000000",
                    "deliveryPolicy": "summary_on_non_match",
                }
            )
            self.assertTrue(result["written"])
            self.assertFalse(result["manualOnly"])
            self.assertTrue((tmp / "config.json").exists())
            self.assertTrue((tmp / "preferences.md").exists())
            payload = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["audibleMarketplace"], "us")
            self.assertEqual(payload["threshold"], 3.8)
            self.assertEqual(payload["stateFile"], str(tmp / "state.json"))
            self.assertEqual(payload["deliveryChannel"], "telegram")
            self.assertEqual(payload["deliveryTarget"], "-1000000000000")
            self.assertEqual(payload["deliveryPolicy"], "summary_on_non_match")

    def test_setup_returns_manual_instructions_when_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            with mock.patch.object(delivery_mod, "write_json_atomic", side_effect=OSError("denied")):
                result = core.setup_configuration({"storageDir": str(tmp), "audibleMarketplace": "us"})
        self.assertFalse(result["written"])
        self.assertTrue(result["manualOnly"])
        self.assertIn('"audibleMarketplace": "us"', result["configJson"])

    def test_setup_cron_registration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            state_path = tmp / "state.json"
            spec = core.validate_marketplace("us")
            expected_message = core.build_cron_message(config_path, state_path)
            existing = {
                "id": "job-1",
                "name": "Audible Goodreads Deal (US)",
                "schedule": {"cron": spec["defaultCron"], "tz": spec["timezone"]},
                "payload": {"message": expected_message},
            }
            with mock.patch.object(delivery_mod, "list_cron_jobs", return_value=[existing]):
                result = core.setup_configuration(
                    {"storageDir": str(tmp), "audibleMarketplace": "us", "dailyAutomation": True},
                    register_cron=True,
                )
        registration = result["cronRegistration"]
        self.assertTrue(registration["ok"])
        self.assertFalse(registration["created"])
        self.assertEqual(registration["existingJob"]["id"], "job-1")

    def test_resolve_delivery_settings_prefers_explicit_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            core.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1",
                },
            )
            resolved_path, channel, target, policy = core.resolve_delivery_settings(
                config_path=config_path,
                delivery_channel="telegram",
                delivery_target="-2",
            )
        self.assertEqual(resolved_path, config_path.resolve())
        self.assertEqual(channel, "telegram")
        self.assertEqual(target, "-2")
        self.assertEqual(policy, core.DEFAULT_DELIVERY_POLICY)

    def test_deliver_message_uses_openclaw_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            core.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1000000000000",
                },
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"payload": {"ok": True, "messageId": "42"}}),
                stderr="",
            )
            with mock.patch.object(delivery_mod.subprocess, "run", return_value=completed) as patched:
                result = core.deliver_message(
                    message_text="hello world",
                    config_path=config_path,
                    openclaw_bin="/fake/openclaw",
                )
        command = patched.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/fake/openclaw",
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                "-1000000000000",
                "--message",
                "hello world",
                "--json",
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["deliveryTarget"], "-1000000000000")
        self.assertEqual(result["payload"]["messageId"], "42")
        self.assertEqual(result["deliveryPolicy"], core.DEFAULT_DELIVERY_POLICY)

    def test_publish_audit_reports_skill_key_and_publish_command(self) -> None:
        args = mock.Mock(version="1.2.3", tags="latest,stable")
        with mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_publish_audit(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        payload = json.loads(output_text)
        self.assertTrue(payload["frontmatter"]["hasLicense"])
        self.assertTrue(payload["frontmatter"]["hasSkillKey"])
        self.assertTrue(payload["frontmatter"]["hasCategory"])
        self.assertTrue(payload["publishIgnore"]["exists"])
        self.assertTrue(payload["publishIgnore"]["requiredExclusionsPresent"])
        self.assertEqual(payload["publishIgnore"]["missingExclusions"], [])
        self.assertTrue(payload["privacyAudit"]["ok"])
        self.assertIn("clawhub skill publish", payload["recommendedPublishCommand"])
        self.assertTrue(payload["recommendedPublishCommand"].startswith("clawhub skill publish . "))

    def test_repo_audit_detects_private_machine_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            leak_text = "run this on " + "hor" + "st via " + "tail" + "scale"
            (tmp / "notes.txt").write_text(leak_text, encoding="utf-8")
            payload = repo_audit.scan_repo_for_leaks(tmp)
        self.assertFalse(payload["ok"])
        markers = {finding["marker"] for finding in payload["findings"]}
        self.assertIn("hor" + "st", markers)
        self.assertIn("tail" + "scale", markers)

    def test_bold_visible_text_styles_ascii_title(self) -> None:
        self.assertEqual(core.bold_visible_text("Signal Fire"), "𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲")

    def test_build_delivery_plan_positive_only_skips_suppressions(self) -> None:
        final_result = {
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "reasonText": "Already marked as read.",
            "message": "full message",
            "audible": {},
            "goodreads": {},
            "metadata": {},
            "warnings": [],
        }
        plan = core.build_delivery_plan(final_result, "positive_only")
        self.assertFalse(plan["shouldDeliver"])
        self.assertEqual(plan["mode"], "skip")

    def test_build_delivery_plan_summary_mode_condenses_suppression(self) -> None:
        final_result = {
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "reasonText": "Already marked as read.",
            "message": "full message",
            "audible": {"title": "Signal Fire", "author": "Jane Story", "year": 2022, "audibleUrl": "https://audible"},
            "goodreads": {"status": "resolved", "url": "https://goodreads", "averageRating": 4.2, "ratingsCount": 1000},
            "metadata": {"marketplace": "us", "marketplaceLabel": "Audible US", "storeLocalDate": "2026-04-20"},
            "warnings": [],
        }
        plan = core.build_delivery_plan(final_result, "summary_on_non_match")
        self.assertTrue(plan["shouldDeliver"])
        self.assertEqual(plan["mode"], "summary")
        self.assertIn("Audible US Daily Promotion — 2026-04-20", plan["message"])
        self.assertIn("𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲 — Jane Story (2022)", plan["message"])
        self.assertIn("Fit: You marked it as read on Goodreads.", plan["message"])
        self.assertIn("Audible: https://audible", plan["message"])
        self.assertNotIn("Result:", plan["message"])
        self.assertNotIn("Reason:", plan["message"])

    def test_build_delivery_plan_summary_mode_condenses_errors(self) -> None:
        final_result = {
            "status": "error",
            "reasonCode": "error_goodreads_lookup_failed",
            "reasonText": "Goodreads public lookup failed.",
            "message": "full error",
            "audible": {"title": "Signal Fire", "author": "Jane Story", "year": 2022},
            "goodreads": {"status": "lookup_failed"},
            "metadata": {"marketplace": "us", "marketplaceLabel": "Audible US", "storeLocalDate": "2026-04-20"},
            "warnings": [],
        }
        plan = core.build_delivery_plan(final_result, "summary_on_non_match")
        self.assertTrue(plan["shouldDeliver"])
        self.assertIn("Fit: Goodreads could not be verified right now.", plan["message"])

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

    def test_core_reexports_canonical_shared_helpers(self) -> None:
        self.assertEqual(core.approx_token_count(""), 0)
        self.assertEqual(core.normalize_review_text("<p>Wait...!!</p>"), "Wait.")

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
            core.parse_audible_chip_genres(html),
            ["Literature & Fiction", "Thought-Provoking", "Fiction"],
        )

    def test_supported_marketplaces_include_non_us_release_target(self) -> None:
        self.assertIn("us", core.SUPPORTED_MARKETPLACES)
        self.assertGreaterEqual(len([key for key in core.SUPPORTED_MARKETPLACES if key != "us"]), 1)

    def test_finalize_recommend_to_read_override_without_goodreads_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="to-read", rating="5")])
            prep = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
            final = core.finalize_skill_result(prep, None)
        self.assertEqual(final["status"], "recommend")
        self.assertEqual(final["reasonCode"], "recommend_to_read_override")
        self.assertIn("Fit: Strong match, on your 'to-read' shelf.", final["message"])

    def test_finalize_to_read_override_keeps_fit_and_goodreads_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="to-read", rating="5")])
            prep = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
            final = core.finalize_skill_result(
                prep,
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "url": "https://www.goodreads.com/book/show/1",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                        "averageRating": 4.25,
                        "ratingsCount": 19806,
                    },
                    "fit": {
                        "status": "written",
                        "sentence": "Fit: Strong match, on your to-read shelf. The book lines up with the kinds of sharp, idea-driven fiction you keep around. The main risk is that its style may be more cerebral than emotionally warm.",
                    },
                },
            )
        self.assertEqual(final["status"], "recommend")
        self.assertEqual(final["reasonCode"], "recommend_to_read_override")
        self.assertIn("Goodreads rating: 4.25 (19,806 ratings)", final["message"])
        self.assertIn("Audible: https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567", final["message"])
        self.assertIn("Goodreads: https://www.goodreads.com/book/show/1", final["message"])
        self.assertLess(
            final["message"].index("Audible: https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"),
            final["message"].index("Goodreads: https://www.goodreads.com/book/show/1"),
        )
        self.assertIn("𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲 — Jane Story (2022)", final["message"])
        self.assertIn("Length: 11:48 hrs", final["message"])
        self.assertIn("Genre: Science Fiction, Thriller", final["message"])
        self.assertIn("Fit: Strong match, on your to-read shelf.", final["message"])
        self.assertNotIn("Recommendation: Yes", final["message"])
        self.assertNotIn("Reason: Saved on your Goodreads to-read shelf.", final["message"])

    def test_finalize_recommend_public_threshold(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        final = core.finalize_skill_result(
            prep,
            {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/1",
                    "title": "Signal Fire",
                    "author": "Jane Story",
                    "averageRating": 4.15,
                    "ratingsCount": "9,501",
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertEqual(final["status"], "recommend")
        self.assertEqual(final["reasonCode"], "recommend_public_threshold")
        self.assertIn("Goodreads rating: 4.15 (9,501 ratings)", final["message"])
        self.assertIn("𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲 — Jane Story (2022)", final["message"])

    def test_message_snapshots_match_expected_layout(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        public_final = core.finalize_skill_result(
            prep,
            {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/1",
                    "title": "Signal Fire",
                    "author": "Jane Story",
                    "averageRating": 4.15,
                    "ratingsCount": "9,501",
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertEqual(public_final["message"], read_message_fixture("recommend_public_threshold.txt"))

        summary_suppress = rendering.build_delivery_plan(
            {
                "status": "suppress",
                "reasonCode": "suppress_already_read",
                "reasonText": "Already marked as read.",
                "message": "full message",
                "audible": {"title": "Signal Fire", "author": "Jane Story", "year": 2022, "audibleUrl": "https://audible"},
                "goodreads": {"status": "resolved", "url": "https://goodreads", "averageRating": 4.2, "ratingsCount": 1000},
                "metadata": {"marketplace": "us", "marketplaceLabel": "Audible US", "storeLocalDate": "2026-04-20"},
                "warnings": [],
            },
            "summary_on_non_match",
        )
        self.assertEqual(summary_suppress["message"], read_message_fixture("summary_suppress_already_read.txt"))

        summary_error = rendering.build_delivery_plan(
            {
                "status": "error",
                "reasonCode": "error_goodreads_lookup_failed",
                "reasonText": "Goodreads public lookup failed.",
                "message": "full error",
                "audible": {"title": "Signal Fire", "author": "Jane Story", "year": 2022},
                "goodreads": {"status": "lookup_failed"},
                "metadata": {"marketplace": "us", "marketplaceLabel": "Audible US", "storeLocalDate": "2026-04-20"},
                "warnings": [],
            },
            "summary_on_non_match",
        )
        self.assertEqual(summary_error["message"], read_message_fixture("summary_error_goodreads_lookup_failed.txt"))

    def test_to_read_message_snapshot_matches_expected_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            export_path = tmp / "goodreads.csv"
            write_rows(export_path, [row(title="Signal Fire", author="Jane Story", shelf="to-read", rating="5")])
            prep = core.prepare_run(
                {
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(export_path),
                },
                fetcher=fake_fetcher,
            )
            final = core.finalize_skill_result(
                prep,
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "url": "https://www.goodreads.com/book/show/1",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                        "averageRating": 4.25,
                        "ratingsCount": 19806,
                    },
                    "fit": {
                        "status": "written",
                        "sentence": "Fit: Strong match, on your to-read shelf. The book lines up with the kinds of sharp, idea-driven fiction you keep around. The main risk is that its style may be more cerebral than emotionally warm.",
                    },
                },
            )
        self.assertEqual(final["message"], read_message_fixture("recommend_to_read_override.txt"))

    def test_run_and_deliver_command_finalizes_then_sends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            prepare_path = tmp / "prepare.json"
            prepare = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
            prepare_path.write_text(json.dumps(prepare), encoding="utf-8")
            runtime_output = {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/1",
                    "title": "Signal Fire",
                    "author": "Jane Story",
                    "averageRating": 4.15,
                },
                "fit": {"status": "not_applicable"},
            }
            runtime_path = tmp / "runtime.json"
            runtime_path.write_text(json.dumps(runtime_output), encoding="utf-8")
            delivered = {"ok": True, "payload": {"ok": True, "messageId": "7"}}
            args = mock.Mock(
                prepare_json=str(prepare_path),
                runtime_output=str(runtime_path),
                config_path=str(tmp / "config.json"),
                delivery_channel=None,
                delivery_target=None,
                openclaw_bin="openclaw",
                dry_run=False,
            )
            with mock.patch.object(core, "deliver_message", return_value=delivered), mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
                rc = public_cli.command_run_and_deliver(args)
                output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        payload = json.loads(output_text)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["delivered"])
        self.assertEqual(payload["delivery"]["payload"]["messageId"], "7")
        self.assertEqual(payload["finalResult"]["reasonCode"], "recommend_public_threshold")

    def test_run_and_deliver_skips_suppression_under_positive_only(self) -> None:
        prepare = {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "message": "Already read.",
            "warnings": [],
            "audible": {"title": "Signal Fire", "author": "Jane Story"},
            "personalData": {},
            "artifacts": {},
            "metadata": {"marketplace": "us"},
        }
        args = mock.Mock(
            prepare_json="-",
            runtime_output=None,
            config_path=None,
            delivery_channel=None,
            delivery_target=None,
            delivery_policy="positive_only",
            openclaw_bin="openclaw",
            dry_run=False,
        )
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(core, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "positive_only")), mock.patch.object(core, "deliver_message") as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        deliver_mock.assert_not_called()
        payload = json.loads(output_text)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(payload["deliveryPlan"]["mode"], "skip")

    def test_run_and_deliver_summary_mode_sends_suppression_summary(self) -> None:
        prepare = {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "message": "Already read.",
            "warnings": [],
            "audible": {"title": "Signal Fire", "author": "Jane Story", "year": 2022, "audibleUrl": "https://audible"},
            "personalData": {},
            "artifacts": {},
            "metadata": {"marketplace": "us", "marketplaceLabel": "Audible US", "storeLocalDate": "2026-04-20"},
        }
        delivered = {"ok": True, "payload": {"ok": True, "messageId": "8"}}
        args = mock.Mock(
            prepare_json="-",
            runtime_output=None,
            config_path=None,
            delivery_channel=None,
            delivery_target=None,
            delivery_policy="summary_on_non_match",
            openclaw_bin="openclaw",
            dry_run=False,
        )
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(core, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "summary_on_non_match")), mock.patch.object(core, "deliver_message", return_value=delivered) as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        self.assertIn("Fit: You marked it as read on Goodreads.", deliver_mock.call_args.kwargs["message_text"])
        self.assertIn("Audible US Daily Promotion — 2026-04-20", deliver_mock.call_args.kwargs["message_text"])
        payload = json.loads(output_text)
        self.assertTrue(payload["delivered"])
        self.assertEqual(payload["deliveryPlan"]["mode"], "summary")

    def test_run_and_deliver_reports_delivery_failure_cleanly(self) -> None:
        prepare = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        args = mock.Mock(
            prepare_json="-",
            runtime_output=None,
            config_path=None,
            delivery_channel="telegram",
            delivery_target="-1",
            delivery_policy="always_full",
            openclaw_bin="openclaw",
            dry_run=False,
        )
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(core, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "always_full")), mock.patch.object(core, "deliver_message", side_effect=RuntimeError("send failed")) as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 1)
        deliver_mock.assert_called_once()
        payload = json.loads(output_text)
        self.assertFalse(payload["ok"])
        self.assertIn("send failed", payload["error"])

    def test_finalize_suppress_below_threshold(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        final = core.finalize_skill_result(
            prep,
            {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/1",
                    "title": "Signal Fire",
                    "author": "Jane Story",
                    "averageRating": 3.7,
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertEqual(final["status"], "suppress")
        self.assertEqual(final["reasonCode"], "suppress_below_goodreads_threshold")

    def test_finalize_suppresses_when_no_goodreads_match(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        final = core.finalize_skill_result(
            prep,
            {
                "schemaVersion": 1,
                "goodreads": {"status": "no_match"},
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertEqual(final["status"], "suppress")
        self.assertEqual(final["reasonCode"], "suppress_no_goodreads_match")

    def test_finalize_errors_when_goodreads_lookup_fails(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us"}, fetcher=fake_fetcher)
        final = core.finalize_skill_result(
            prep,
            {
                "schemaVersion": 1,
                "goodreads": {"status": "lookup_failed"},
                "fit": {"status": "unavailable"},
            },
        )
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["reasonCode"], "error_goodreads_lookup_failed")

    def test_finalize_uses_model_unavailable_fallback_for_personalized_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            prep = core.prepare_run(
                {
                    "audibleMarketplace": "us",
                    "artifactDir": str(tmp / "artifacts"),
                    "notesText": "I like quiet literary science fiction.",
                },
                fetcher=fake_fetcher,
            )
            final = core.finalize_skill_result(
                prep,
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "url": "https://www.goodreads.com/book/show/1",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                        "averageRating": 4.2,
                    },
                    "fit": {"status": "unavailable"},
                },
            )
        self.assertEqual(final["fitSentence"], core.FIT_MODEL_UNAVAILABLE)

    def test_end_to_end_contract_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            prep = core.prepare_run(
                {
                    "audibleMarketplace": "us",
                    "artifactDir": str(tmp / "artifacts"),
                    "notesText": "I like bold speculative fiction.",
                },
                fetcher=fake_fetcher,
            )
            runtime_path = tmp / "runtime-output.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "goodreads": {
                            "status": "resolved",
                            "url": "https://www.goodreads.com/book/show/1",
                            "title": "Signal Fire",
                            "author": "Jane Story",
                            "averageRating": 4.25,
                        },
                        "fit": {
                            "status": "written",
                            "sentence": "Likely fit because it lines up with the speculative fiction preferences in your notes.",
                        },
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "audible_goodreads_deal_scout.harness",
                    "--prepare-json",
                    prep["artifacts"]["prepareResultPath"],
                    "--runtime-output",
                    str(runtime_path),
                    "--expect-status",
                    "recommend",
                    "--expect-reason",
                    "recommend_public_threshold",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "recommend")
        self.assertIn("Fit:", payload["message"])


if __name__ == "__main__":
    unittest.main()
