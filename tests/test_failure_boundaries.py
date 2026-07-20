from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audible_goodreads_deal_scout import audible_auth, audible_catalog, core, delivery, settings, shared  # noqa: E402


class FailureBoundaryTests(unittest.TestCase):
    def test_store_date_respects_pacific_midnight_across_dst_transitions(self) -> None:
        spec = settings.validate_marketplace("us")
        cases = (
            (datetime(2026, 3, 8, 7, 59, tzinfo=UTC), date(2026, 3, 7)),
            (datetime(2026, 3, 8, 8, 1, tzinfo=UTC), date(2026, 3, 8)),
            (datetime(2026, 11, 1, 6, 59, tzinfo=UTC), date(2026, 10, 31)),
            (datetime(2026, 11, 1, 7, 1, tzinfo=UTC), date(2026, 11, 1)),
        )
        for now_utc, expected in cases:
            with self.subTest(now_utc=now_utc):
                self.assertEqual(core.logical_store_date(spec, now_utc=now_utc), expected)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            core.logical_store_date(spec, now_utc=datetime(2026, 3, 8, 8, 1))

    def test_auth_expiry_boundary_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            base_payload = {
                "schemaVersion": 1,
                "purpose": "audible_member_price_lookup",
                "status": "ready",
                "marketplace": "us",
                "domain": "com",
                "refreshToken": "test-only-refresh-token",
                "accessToken": "test-only-access-token",
            }
            for expires, expected_expired in ((1000, True), (1001, False)):
                auth_path.write_text(json.dumps({**base_payload, "expires": expires}), encoding="utf-8")
                os.chmod(auth_path, 0o600)
                with mock.patch.object(audible_auth.time, "time", return_value=1000):
                    status = audible_auth.auth_file_status(auth_path)
                self.assertIs(status["expired"], expected_expired)

    def test_catalog_cache_ttl_boundary_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            client = audible_catalog.AudibleCatalogClient(cache_dir=cache_dir, request_delay=0)
            cache_path = client._cache_path("search", "Signal Fire Jane Story")
            now = datetime(2026, 7, 19, 12, tzinfo=UTC)
            payload = {"ok": True, "cards": []}

            cache_path.write_text(
                json.dumps(
                    {
                        "fetchedAt": (now - timedelta(seconds=audible_catalog.SEARCH_TTL_SECONDS)).isoformat(),
                        **payload,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(audible_catalog, "utc_now", return_value=now):
                self.assertIsNotNone(client._read_cache("search", "Signal Fire Jane Story", audible_catalog.SEARCH_TTL_SECONDS))

            cache_path.write_text(
                json.dumps(
                    {
                        "fetchedAt": (
                            now - timedelta(seconds=audible_catalog.SEARCH_TTL_SECONDS, microseconds=1)
                        ).isoformat(),
                        **payload,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(audible_catalog, "utc_now", return_value=now):
                self.assertIsNone(client._read_cache("search", "Signal Fire Jane Story", audible_catalog.SEARCH_TTL_SECONDS))

            cache_path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(client._read_cache("search", "Signal Fire Jane Story", audible_catalog.SEARCH_TTL_SECONDS))

    def test_atomic_write_failure_preserves_original_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "state.json"
            target.write_text("original\n", encoding="utf-8")
            with mock.patch.object(shared.os, "replace", side_effect=OSError("simulated interruption")):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    shared.atomic_write_text(target, "replacement\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    def test_delivery_surfaces_timeout_and_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps({"deliveryChannel": "telegram", "deliveryTarget": "books"}),
                encoding="utf-8",
            )
            timeout = subprocess.TimeoutExpired(["openclaw"], 60)
            with mock.patch.object(delivery.subprocess, "run", side_effect=timeout):
                with self.assertRaisesRegex(RuntimeError, "timed out after 60 seconds"):
                    delivery.deliver_message(message_text="test", config_path=config_path)

            completed = subprocess.CompletedProcess(["openclaw"], 0, stdout="not-json", stderr="")
            with mock.patch.object(delivery.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "returned invalid JSON"):
                    delivery.deliver_message(message_text="test", config_path=config_path)

    def test_invalid_cron_json_cannot_be_treated_as_an_empty_job_list(self) -> None:
        completed = subprocess.CompletedProcess(["openclaw"], 0, stdout="not-json", stderr="")
        with mock.patch.object(delivery.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "openclaw cron list returned invalid JSON"):
                delivery.list_cron_jobs("openclaw")


if __name__ == "__main__":
    unittest.main()
