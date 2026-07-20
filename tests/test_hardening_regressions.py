from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import random
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audible_goodreads_deal_scout import audible_auth, audible_catalog, audible_fetch, core, delivery, diagnostics
from audible_goodreads_deal_scout import public_cli, repo_audit, settings, shared, want_to_read_scan
from audible_goodreads_deal_scout.goodreads_rating import (
    GoodreadsRatingError,
    SafeGoodreadsRedirectHandler,
    parse_goodreads_rating,
)
from helpers import fake_fetcher, scan_row, write_rows


def valid_auth_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "purpose": "audible_member_price_lookup",
        "status": "ready",
        "marketplace": "us",
        "domain": "com",
        "refreshToken": "refresh-old",
        "accessToken": "access-old",
        "expires": 0,
    }
    payload.update(overrides)
    return payload


class SecurityHardeningTests(unittest.TestCase):
    def test_nested_auth_payload_redaction_is_category_based(self) -> None:
        payload = {
            "website_cookies": ["session-secret"],
            "mac_dms": {"adp_token": "adp-secret", "device_private_key": "key-secret"},
            "customer_info": {"email": "private@example.com"},
            "safe": "visible",
        }
        redacted = shared.redact_sensitive_payload(payload)
        serialized = json.dumps(redacted)
        for secret in ("session-secret", "adp-secret", "key-secret", "private@example.com"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(redacted["safe"], "visible")

    def test_auth_refresh_persists_rotated_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "auth.json"
            shared.write_json_atomic(path, valid_auth_payload())
            os.chmod(path, 0o600)
            response = {"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600}
            with mock.patch.object(audible_auth, "_post_form", return_value=response):
                refreshed = audible_auth.refresh_access_token(path, force=True)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["refreshToken"], "refresh-new")
        self.assertEqual(saved["refreshToken"], "refresh-new")

    def test_authenticated_requests_refuse_redirects(self) -> None:
        handler = audible_auth._NoRedirectHandler()
        request = mock.Mock()
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://example.invalid")
        self.assertIsNone(redirected)

    def test_public_fetch_redirects_are_validated_before_following(self) -> None:
        with self.assertRaises(audible_fetch.AudibleFetchError):
            audible_fetch.SafeAudibleRedirectHandler().redirect_request(
                mock.Mock(), None, 302, "Found", {}, "https://example.invalid/dailydeal"
            )
        for url in (
            "https://example.invalid/book/show/1",
            "https://www.goodreads.com:8443/book/show/1",
        ):
            with self.assertRaises(GoodreadsRatingError):
                SafeGoodreadsRedirectHandler().redirect_request(mock.Mock(), None, 302, "Found", {}, url)
        self.assertNotIn("--location", audible_fetch._curl_command("https://www.audible.com/dailydeal", "curl"))

    def test_auth_permission_fix_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "target.json"
            shared.write_json_atomic(target, valid_auth_payload())
            os.chmod(target, 0o644)
            link = root / "auth.json"
            link.symlink_to(target)
            result = audible_auth.auth_file_status(link, fix_permissions=True)
            target_mode = target.stat().st_mode & 0o777
        self.assertFalse(result["ok"])
        self.assertFalse(result["ready"])
        self.assertEqual(target_mode, 0o644)

    def test_response_reader_rejects_oversized_payload(self) -> None:
        response = mock.Mock()
        response.read.return_value = b"x" * 33
        with self.assertRaisesRegex(ValueError, "safety limit"):
            shared.read_limited_bytes(response, limit=32)

    def test_private_marker_environment_does_not_echo_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "note.txt").write_text("account-internal-marker", encoding="utf-8")
            with mock.patch.dict(os.environ, {repo_audit.PRIVATE_MARKERS_ENV: "account-internal-marker"}):
                result = repo_audit.scan_repo_for_leaks(root)
        self.assertFalse(result["ok"])
        self.assertNotIn("account-internal-marker", json.dumps(result))


class ConfigDeliveryStateTests(unittest.TestCase):
    def test_noninteractive_setup_preserves_marketplace_and_unknown_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            shared.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "uk",
                    "threshold": 4.2,
                    "scanOrder": "oldest",
                    "maxRequests": 7,
                },
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = public_cli.main(["setup", "--non-interactive", "--config-path", str(config_path)])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(saved["audibleMarketplace"], "uk")
        self.assertEqual(saved["threshold"], 4.2)
        self.assertEqual(saved["scanOrder"], "oldest")
        self.assertEqual(saved["maxRequests"], 7)

    def test_setup_refuses_to_overwrite_malformed_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text("{broken", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = public_cli.main(["setup", "--non-interactive", "--config-path", str(config_path)])
            self.assertEqual(config_path.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(rc, 1)
        self.assertIn("not readable JSON", stdout.getvalue())

    def test_delivery_overrides_must_be_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            shared.write_json_atomic(
                config_path,
                {"deliveryChannel": "telegram", "deliveryTarget": "books"},
            )
            with self.assertRaisesRegex(RuntimeError, "must be passed together"):
                delivery.resolve_delivery_settings(config_path=config_path, delivery_channel="slack")

    def test_delivery_rejects_success_exit_with_failure_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            shared.write_json_atomic(
                config_path,
                {"deliveryChannel": "telegram", "deliveryTarget": "books"},
            )
            proc = subprocess.CompletedProcess([], 0, json.dumps({"ok": False, "error": "rejected"}), "")
            with mock.patch.object(delivery, "_run_openclaw", return_value=proc):
                with self.assertRaisesRegex(RuntimeError, "rejected"):
                    delivery.deliver_message(message_text="hello", config_path=config_path)

    def test_run_and_deliver_dry_run_is_not_reported_as_delivered(self) -> None:
        args = SimpleNamespace(
            prepare_json="-",
            runtime_output=None,
            config_path=None,
            delivery_channel="telegram",
            delivery_target="books",
            delivery_policy="always_full",
            openclaw_bin="openclaw",
            dry_run=True,
        )
        final = {"status": "recommend", "reasonCode": "recommend_public_threshold", "message": "hello"}
        with (
            mock.patch.object(public_cli, "load_json_input", return_value={}),
            mock.patch.object(core, "scheduled_prepare_rejection", return_value=None),
            mock.patch.object(core, "finalize_skill_result", return_value=final),
            mock.patch.object(public_cli, "resolve_delivery_policy", return_value=(Path("/tmp/cfg"), "always_full")),
            mock.patch.object(
                public_cli,
                "build_delivery_plan",
                return_value={"shouldDeliver": True, "mode": "full", "message": "hello"},
            ),
            mock.patch.object(
                public_cli,
                "deliver_message",
                return_value={"ok": True, "delivered": False, "simulated": True},
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            rc = public_cli.command_run_and_deliver(args)
        result = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0)
        self.assertFalse(result["delivered"])
        self.assertTrue(result["simulated"])

    def test_scheduled_delivery_rejects_different_config(self) -> None:
        args = SimpleNamespace(
            prepare_json="-",
            runtime_output=None,
            config_path="/tmp/requested-config.json",
            delivery_channel=None,
            delivery_target=None,
            delivery_policy=None,
            openclaw_bin="openclaw",
            dry_run=False,
        )
        prep = {
            "metadata": {"invocationMode": "scheduled", "configPath": "/tmp/artifact-config.json"}
        }
        with (
            mock.patch.object(public_cli, "load_json_input", return_value=prep),
            mock.patch.object(core, "scheduled_prepare_rejection", return_value=None),
            mock.patch.object(core, "finalize_skill_result", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "refused config"):
                public_cli.command_run_and_deliver(args)

    def test_cron_commands_enable_light_context_trigger_and_clear_old_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "config.json"
            state_path = root / "state.json"
            shared.write_json_atomic(config_path, {"audibleMarketplace": "us"})
            spec = settings.validate_marketplace("us")
            add = delivery.build_cron_command(
                openclaw_bin="/openclaw",
                spec=spec,
                config_path=config_path,
                state_file=state_path,
            )
            edit = delivery.build_cron_edit_command(
                openclaw_bin="/openclaw",
                job_id="job-1",
                spec=spec,
                config_path=config_path,
                state_file=state_path,
                name="Audible",
                cron_expr="0 12 * * *",
            )
        self.assertIn("--light-context", add)
        self.assertEqual(add[add.index("--thinking") + 1], "off")
        self.assertIn("--trigger-script", add)
        self.assertIn("--declaration-key", add)
        self.assertIn("--no-deliver", add)
        self.assertNotIn("--announce", add)
        self.assertNotIn("$audible-goodreads-deal-scout", delivery.build_cron_message(config_path, state_path))
        for flag in ("--no-deliver", "--clear-channel", "--clear-to"):
            self.assertIn(flag, edit)

    def test_corrupt_state_fails_closed_and_wrong_state_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            state_path.write_text("{broken", encoding="utf-8")
            result = core.prepare_run(
                {
                    "audibleMarketplace": "us",
                    "artifactDir": str(root / "artifacts"),
                    "stateFile": str(state_path),
                    "invocationMode": "scheduled",
                },
                fetcher=fake_fetcher,
            )
            self.assertEqual(result["reasonCode"], "error_state_unreadable")

            state_path.unlink()
            prep = core.prepare_run(
                {
                    "audibleMarketplace": "us",
                    "artifactDir": str(root / "artifacts"),
                    "stateFile": str(state_path),
                    "invocationMode": "scheduled",
                },
                fetcher=fake_fetcher,
            )
            with self.assertRaisesRegex(ValueError, "refused state file"):
                core.mark_emitted_from_prepare(root / "other-state.json", prep)

    def test_doctor_fails_missing_configured_optional_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "config.json"
            shared.write_json_atomic(config_path, {"goodreadsCsvPath": "missing.csv"})
            with mock.patch.object(diagnostics, "resolve_openclaw_bin", return_value=os.sys.executable):
                result = diagnostics.doctor_report(config_path=config_path, openclaw_bin=os.sys.executable)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["csv"]["status"], "missing")

    def test_setup_can_disable_existing_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "config.json"
            shared.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "stateFile": str(root / "state.json"),
                    "dailyCron": "0 12 * * *",
                },
            )
            with mock.patch.object(
                delivery,
                "disable_related_cron_job",
                return_value={"ok": True, "disabled": True},
            ) as disabled:
                result = delivery.setup_configuration(
                    {"configPath": str(config_path), "dailyAutomation": False},
                    register_cron=True,
                )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(result["cronRegistration"]["disabled"])
        disabled.assert_called_once()
        self.assertIsNone(saved["stateFile"])
        self.assertIsNone(saved["dailyCron"])


class ScanAndParserHardeningTests(unittest.TestCase):
    def test_redirect_attempt_does_not_mask_terminal_fetch_failure(self) -> None:
        metadata = core.fetch_metadata_from_attempts(
            [
                {
                    "backend": "curl",
                    "ok": True,
                    "httpStatus": 302,
                    "reasonCode": "safe_redirect_followed",
                },
                {
                    "backend": "curl",
                    "ok": False,
                    "httpStatus": 503,
                    "reasonCode": "http_503_curl_fetch_failed",
                },
            ]
        )
        self.assertEqual(metadata["httpStatus"], 503)
        self.assertEqual(metadata["reasonCode"], "http_503_curl_fetch_failed")

    def test_zero_request_budget_performs_no_network_fetch(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> tuple[str, str]:
            calls.append(url)
            raise AssertionError("network must not be called")

        report, _, rc = want_to_read_scan.scan_want_to_read(
            {
                "title": "Signal Fire",
                "author": "Jane Story",
                "maxRequests": 0,
                "noCache": True,
                "progress": "none",
                "enrichGoodreadsRatings": False,
            },
            fetcher=fetcher,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(report["reasonCode"], "request_budget_exhausted")
        self.assertEqual(calls, [])

    def test_repeated_ordinary_failures_return_partial_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            csv_path = root / "books.csv"
            write_rows(
                csv_path,
                [scan_row(str(index), f"Book {index}", "Author", "2026/04/05") for index in range(7)],
            )
            config_path = root / "config.json"
            shared.write_json_atomic(config_path, {"goodreadsCsvPath": str(csv_path)})

            def failed_fetch(url: str) -> tuple[str, str]:
                raise RuntimeError("offline")

            report, _, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "maxRequests": 20,
                    "requestDelay": 0,
                    "noCache": True,
                    "progress": "none",
                    "enrichGoodreadsRatings": False,
                },
                fetcher=failed_fetch,
            )
        self.assertEqual(rc, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reasonCode"], "ordinary_fetch_failure_limit")

    def test_candidate_order_always_prefers_strong_match(self) -> None:
        book = {"title": "Signal Fire", "author": "Jane Story"}
        cards = [
            {"title": "Signal Fire", "author": "Wrong Author", "url": "https://www.audible.com/pd/X/B000000001", "offer": {}},
            {"title": "Signal Fire", "author": "Jane Story", "url": "https://www.audible.com/pd/X/B000000002", "offer": {}},
            {"title": "Other", "author": "Jane Story", "url": "https://www.audible.com/pd/X/B000000003", "offer": {}},
        ]
        for permutation in itertools.permutations(cards):
            with tempfile.TemporaryDirectory() as tmp_dir:
                client = audible_catalog.AudibleCatalogClient(
                    cache_dir=Path(tmp_dir),
                    max_requests=1,
                    request_delay=0,
                    no_cache=True,
                    fetcher=lambda url: ("html", url),
                )
                with mock.patch.object(audible_catalog, "parse_search_cards", return_value=list(permutation)):
                    result = client.search_book(book, min_discount_percent=10)
            self.assertEqual(result["audible"]["author"], "Jane Story")

    def test_goodreads_json_ld_order_and_entity_variants(self) -> None:
        organization = {"@type": "Organization", "aggregateRating": {"ratingValue": "1.1", "ratingCount": "5"}}
        book = {
            "@type": "Book",
            "name": "A &quot;Quoted&quot; Book",
            "aggregateRating": {"ratingValue": "4.7", "ratingCount": "1000"},
        }
        for payloads in itertools.permutations((organization, book)):
            html = "".join(
                f'<script type="application/ld+json">{json.dumps(payload)}</script>' for payload in payloads
            )
            parsed = parse_goodreads_rating(html)
            self.assertEqual(parsed["averageRating"], 4.7)
            self.assertEqual(parsed["ratingsCount"], 1000)

    def test_goodreads_rating_rejects_nonfinite_and_out_of_range_values(self) -> None:
        for value in ("-0.1", "5.1", "NaN", "Infinity"):
            payload = {"@type": "Book", "aggregateRating": {"ratingValue": value}}
            html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
            with self.subTest(value=value):
                with self.assertRaises(GoodreadsRatingError):
                    parse_goodreads_rating(html)

    def test_goodreads_rating_does_not_use_non_book_json_ld(self) -> None:
        payload = {"@type": "Organization", "aggregateRating": {"ratingValue": "4.9"}}
        html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        with self.assertRaises(GoodreadsRatingError):
            parse_goodreads_rating(html)

    def test_relative_config_paths_resolve_against_config_directory(self) -> None:
        rng = random.Random(7)
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config" / "config.json"
            for _ in range(30):
                relative = Path("data") / f"item-{rng.randrange(10_000)}.json"
                resolved = settings.resolve_configured_path(config_path, str(relative))
                self.assertEqual(resolved, (config_path.parent / relative).resolve())


class ReleaseAndSchedulingTests(unittest.TestCase):
    def test_publish_audit_covers_every_python_module_and_rejects_version_drift(self) -> None:
        current_args = SimpleNamespace(version=public_cli.__version__, tags="latest")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            current_rc = public_cli.command_publish_audit(current_args)
        current = json.loads(stdout.getvalue())
        package_dir = Path(public_cli.__file__).resolve().parent
        self.assertEqual(current_rc, 0)
        self.assertEqual(current["publishBundle"]["runtimeModuleCount"], len(list(package_dir.glob("*.py"))))
        self.assertTrue(current["publishBundle"]["requiredRuntimeFilesIncluded"])

        drift_args = SimpleNamespace(version="999.0.0", tags="latest")
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            drift_rc = public_cli.command_publish_audit(drift_args)
        self.assertEqual(drift_rc, 1)
        self.assertIn("does not match package version", stdout.getvalue())

    def test_scheduled_gate_skips_positive_only_deterministic_suppression(self) -> None:
        prep = {
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "metadata": {"storeLocalDate": "2026-07-19"},
            "artifacts": {"prepareResultPath": "/tmp/prepare.json"},
        }
        args = SimpleNamespace(config_path="/tmp/config.json", state_file="/tmp/state.json")
        with (
            mock.patch.object(core, "prepare_run", return_value=prep),
            mock.patch.object(public_cli, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "positive_only")),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            rc = public_cli.command_scheduled_gate(args)
        result = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0)
        self.assertFalse(result["fire"])

    def test_generated_trigger_calls_gate_without_embedding_secrets(self) -> None:
        script = delivery.build_scheduled_trigger_script(Path("/tmp/config.json"), Path("/tmp/state.json"))
        self.assertIn("scheduled-gate", script)
        self.assertIn("Boolean(gate.fire)", script)
        self.assertNotIn("deliveryTarget", script)


if __name__ == "__main__":
    unittest.main()
