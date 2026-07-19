from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import (  # noqa: E402
    public_cli,
    settings,
)
from audible_goodreads_deal_scout import audible_fetch  # noqa: E402
from audible_goodreads_deal_scout import audible_auth  # noqa: E402
from audible_goodreads_deal_scout import audible_catalog  # noqa: E402
from audible_goodreads_deal_scout import diagnostics  # noqa: E402
from audible_goodreads_deal_scout import delivery as delivery_mod  # noqa: E402
from audible_goodreads_deal_scout import want_to_read_scan  # noqa: E402
from helpers import (  # noqa: E402
    audible_search_card,
    scan_row,
    write_rows,
    write_want_to_read_fixtures,
)


class AuthDiagnosticsTests(unittest.TestCase):
    def test_search_card_without_author_does_not_fetch_product_for_identity_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us"}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Authorless Book Jane Story": f"<ol>{audible_search_card('Authorless Book', '', 'B000000007', 'Regular Price: $14.95 Sale Price: $4.99')}</ol>",
                },
                product={},
            )
            report, _markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "title": "Authorless Book",
                    "author": "Jane Story",
                    "offlineFixtures": str(fixtures),
                    "requestDelay": 0,
                    "maxRequests": 5,
                }
            )
        self.assertEqual(rc, 0)
        self.assertEqual(report["requestBudget"]["used"], 1)
        self.assertEqual(report["results"][0]["status"], "needs_review")

    def test_search_parser_reads_live_like_nested_author_block(self) -> None:
        html = """
        <ul>
          <li class="bc-list-item productListItem" id="product-list-item-1984887467" aria-label="The Scout Mindset">
            <a href="/pd/The-Scout-Mindset-Audiobook/1984887467?qid=1">
              <img alt="The Scout Mindset Audiobook By Julia Galef cover art" />
            </a>
            <div id="product-list-flyout-1984887467">
              <ul>
                <li><h2>The Scout Mindset</h2></li>
                <li>Why Some People See Things Clearly and Others Don't</li>
                <li>
                  By:
                  Julia Galef
                </li>
                <li>Unabridged</li>
              </ul>
            </div>
          </li>
          <li class="bc-list-item productListItem" id="product-list-item-0000000000">
            <a href="/pd/Other-Audiobook/0000000000">Other</a>
          </li>
        </ul>
        """
        cards = audible_catalog.parse_search_cards(html)
        self.assertEqual(cards[0]["title"], "The Scout Mindset")
        self.assertEqual(cards[0]["author"], "Julia Galef")
        self.assertNotIn("abridged", cards[0]["warnings"])

    def test_audible_auth_start_and_finish_external_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            started = audible_auth.start_external_auth(auth_path, marketplace="us")
            pending = json.loads(auth_path.read_text(encoding="utf-8"))
            redirect_url = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=AUTHCODE"
            register_payload = {
                "response": {
                    "success": {
                        "tokens": {
                            "bearer": {
                                "access_token": "access-1",
                                "refresh_token": "refresh-1",
                                "expires_in": "3600",
                            }
                        },
                        "extensions": {
                            "device_info": {"serial": pending["serial"]},
                            "customer_info": {"name": "Test User"},
                        },
                    }
                }
            }
            with mock.patch.object(audible_auth, "_post_json", return_value=register_payload) as post_json:
                finished = audible_auth.finish_external_auth(auth_path, redirect_url=redirect_url)
            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            if os.name == "posix":
                saved_mode = auth_path.stat().st_mode & 0o777
        self.assertTrue(started["loginUrl"].startswith("https://www.amazon.com/ap/signin?"))
        self.assertEqual(finished["marketplace"], "us")
        self.assertEqual(saved["status"], "ready")
        self.assertEqual(saved["accessToken"], "access-1")
        self.assertEqual(saved["refreshToken"], "refresh-1")
        self.assertIn("/auth/register", post_json.call_args.args[0])
        self.assertEqual(saved["purpose"], "audible_member_price_lookup")
        self.assertEqual(saved["credentialMetadata"]["allowedUse"], "audible_product_price_lookup_only")
        if os.name == "posix":
            self.assertEqual(saved_mode, 0o600)

    def test_audible_auth_status_reports_expiry_and_fixes_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "com",
                        "refreshToken": "refresh-secret",
                        "accessToken": "access-secret",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(auth_path, 0o644)
                insecure = audible_auth.auth_file_status(auth_path)
                self.assertFalse(insecure["ok"])
                self.assertFalse(insecure["permissionSecure"])
                fixed = audible_auth.auth_file_status(auth_path, fix_permissions=True)
                self.assertTrue(fixed["ok"])
                self.assertEqual(auth_path.stat().st_mode & 0o777, 0o600)
            else:
                fixed = audible_auth.auth_file_status(auth_path)
        self.assertTrue(fixed["ready"])
        self.assertEqual(fixed["credentialMetadata"]["allowedUse"], "audible_product_price_lookup_only")
        self.assertTrue(fixed["credentialMetadata"]["requestsCookieStyleCredentials"])
        self.assertFalse(fixed["credentialMetadata"]["persistsCookieStyleCredentials"])
        self.assertNotIn("access-secret", json.dumps(fixed))

    def test_audible_auth_status_rejects_tampered_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "example.com",
                        "refreshToken": "refresh-secret",
                        "accessToken": "access-secret",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(auth_path, 0o600)
            status = audible_auth.auth_file_status(auth_path)
        self.assertFalse(status["ok"])
        self.assertIn("marketplace/domain mismatch", " ".join(status["errors"]))
        self.assertNotIn("refresh-secret", json.dumps(status))

    def test_authenticated_product_pricing_restricts_product_id_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "com",
                        "refreshToken": "refresh-secret",
                        "accessToken": "access-secret",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(auth_path, 0o600)
            with mock.patch.object(audible_auth, "_get_json", return_value={"product": {"price": {"amount": "7.95"}}}) as get_json:
                pricing = audible_auth.authenticated_product_pricing(auth_path, "b0b6qgtnvs")
            with mock.patch.object(audible_auth, "refresh_access_token") as refresh:
                with self.assertRaises(audible_auth.AudibleAuthError):
                    audible_auth.authenticated_product_pricing(auth_path, "B0B6QGTNVS/../../x")
        called_url = get_json.call_args.args[0]
        self.assertTrue(called_url.startswith("https://api.audible.com/1.0/catalog/products/B0B6QGTNVS?"))
        self.assertIn("response_groups=price", called_url)
        self.assertEqual(pricing["source"], "audible_api_authenticated")
        refresh.assert_not_called()

    def test_authenticated_product_pricing_rejects_tampered_auth_domain_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_path = Path(tmp_dir) / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "example.com",
                        "refreshToken": "refresh-secret",
                        "accessToken": "access-secret",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(auth_path, 0o600)
            with mock.patch.object(audible_auth, "_post_form") as post_form:
                with mock.patch.object(audible_auth, "_get_json") as get_json:
                    with self.assertRaises(audible_auth.AudibleAuthError):
                        audible_auth.authenticated_product_pricing(auth_path, "B0B6QGTNVS")
        post_form.assert_not_called()
        get_json.assert_not_called()

    def test_doctor_report_checks_config_inputs_auth_and_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            write_rows(csv_path, [scan_row("1", "Deal Book", "Jane Story", "2026/04/05")])
            notes_path = tmp / "notes.md"
            notes_path.write_text("Likes concise sci-fi.\n", encoding="utf-8")
            auth_path = tmp / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "com",
                        "refreshToken": "refresh-secret",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(auth_path, 0o600)
            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "audibleMarketplace": "us",
                        "goodreadsCsvPath": str(csv_path),
                        "preferencesPath": str(notes_path),
                        "audibleAuthPath": str(auth_path),
                        "deliveryChannel": "telegram",
                        "deliveryTarget": "@books",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(diagnostics, "curl_available", return_value=True):
                report = diagnostics.doctor_report(config_path=config_path, openclaw_bin=sys.executable)
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["audibleFetchBackend"]["backend"], "auto")
        self.assertTrue(report["checks"]["audibleFetchBackend"]["curlAvailable"])
        self.assertEqual(report["checks"]["csv"]["status"], "ok")
        self.assertTrue(report["checks"]["auth"]["ready"])
        self.assertEqual(report["checks"]["delivery"]["status"], "configured")

    def test_doctor_report_surfaces_disabled_live_cron_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            state_file = tmp / "state.json"
            spec = settings.validate_marketplace("us")
            config_path.write_text(
                json.dumps(
                    {
                        "audibleMarketplace": "us",
                        "dailyCron": spec["defaultCron"],
                        "stateFile": str(state_file),
                    }
                ),
                encoding="utf-8",
            )
            message = delivery_mod.build_cron_message(config_path.resolve(), state_file)
            jobs = [
                {
                    "id": "job-disabled",
                    "name": "Daily Audible deal watch",
                    "enabled": False,
                    "schedule": {"expr": spec["defaultCron"], "tz": spec["timezone"]},
                    "payload": {"message": message},
                }
            ]
            with (
                mock.patch.object(diagnostics, "curl_available", return_value=True),
                mock.patch.object(diagnostics, "list_cron_jobs", return_value=jobs),
            ):
                report = diagnostics.doctor_report(
                    config_path=config_path,
                    openclaw_bin=sys.executable,
                    check_live_cron=True,
                )
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"]["cron"]["status"], "disabled")
        self.assertEqual(report["checks"]["cron"]["disabledMatches"][0]["id"], "job-disabled")
        self.assertEqual(report["errors"], ["cron: disabled"])

    def test_doctor_report_flags_active_cron_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            state_file = tmp / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "audibleMarketplace": "us",
                        "dailyCron": "0 12 * * *",
                        "stateFile": str(state_file),
                        "deliveryChannel": "telegram",
                        "deliveryTarget": "-1000000000000",
                    }
                ),
                encoding="utf-8",
            )
            message = delivery_mod.build_cron_message(config_path.resolve(), state_file)
            jobs = [
                {
                    "id": "job-drifted",
                    "name": "Daily Audible deal watch",
                    "enabled": True,
                    "schedule": {"expr": "0 12 * * *", "tz": "Europe/Lisbon"},
                    "delivery": {"channel": "telegram", "to": "-5038675285"},
                    "payload": {"message": message},
                }
            ]
            with (
                mock.patch.object(diagnostics, "curl_available", return_value=True),
                mock.patch.object(diagnostics, "list_cron_jobs", return_value=jobs),
            ):
                report = diagnostics.doctor_report(
                    config_path=config_path,
                    openclaw_bin=sys.executable,
                    check_live_cron=True,
                )
        match = report["checks"]["cron"]["matches"][0]
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"]["cron"]["status"], "mismatch")
        self.assertFalse(match["timezoneMatchesMarketplace"])
        self.assertFalse(match["deliveryMatchesConfig"])

    def test_doctor_report_can_check_live_audible_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us"}), encoding="utf-8")
            fetch_result = audible_fetch.AudibleFetchResult(
                "html",
                "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567",
                backend="curl",
                attempts=[{"backend": "python", "ok": False}, {"backend": "curl", "ok": True}],
                warnings=["recovered"],
            )
            with (
                mock.patch.object(diagnostics, "curl_available", return_value=True),
                mock.patch.object(diagnostics, "fetch_text_with_final_url", return_value=fetch_result) as fetch,
            ):
                report = diagnostics.doctor_report(
                    config_path=config_path,
                    openclaw_bin=sys.executable,
                    check_audible_fetch=True,
                )
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["audibleFetchLive"]["status"], "ok")
        self.assertEqual(report["checks"]["audibleFetchLive"]["backend"], "curl")
        self.assertEqual(report["warnings"], ["audibleFetchLive: recovered"])
        fetch.assert_called_once_with("https://www.audible.com/dailydeal", retries=0, backend="auto")

    def test_cli_errors_are_structured_and_redacted(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(public_cli, "command_show_csv_headers", side_effect=RuntimeError("Bearer abc123 access_token: secret-token")):
            with mock.patch("sys.stdout", stdout):
                rc = public_cli.main(["show-csv-headers", "missing.csv"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["command"], "show-csv-headers")
        self.assertNotIn("abc123", stdout.getvalue())
        self.assertNotIn("secret-token", stdout.getvalue())

    def test_parse_authenticated_pricing_detects_discount(self) -> None:
        pricing = audible_auth.parse_authenticated_pricing(
            {
                "product": {
                    "asin": "B000000001",
                    "price": {
                        "credit_price": 1.0,
                        "currency_code": "USD",
                        "list_price": {"base": 14.95},
                        "lowest_price": {"base": 4.99},
                    },
                }
            }
        )
        self.assertEqual(pricing["pricingStatus"], "discounted")
        self.assertEqual(pricing["currentPrice"], 4.99)
        self.assertEqual(pricing["listPrice"], 14.95)
        self.assertEqual(pricing["discountPercent"], 67)
        self.assertEqual(pricing["priceBasis"], "audible_member_cash")
        self.assertEqual(pricing["dealType"], "member_cash_below_list")

    def test_authenticated_price_lookup_updates_scan_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us"}), encoding="utf-8")
            auth_path = tmp / "audible-auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "marketplace": "us",
                        "domain": "com",
                        "refreshToken": "refresh",
                        "accessToken": "access",
                        "expires": 4_102_444_800,
                    }
                ),
                encoding="utf-8",
            )
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001')}</ol>",
                },
            )
            pricing_payload = {
                "product": {
                    "price": {
                        "currency_code": "USD",
                        "list_price": {"base": 20.0},
                        "lowest_price": {"base": 5.0},
                    }
                }
            }
            with mock.patch.object(audible_auth, "_get_json", return_value=pricing_payload):
                report, markdown, rc = want_to_read_scan.scan_want_to_read(
                    {
                        "configPath": str(config_path),
                        "title": "Deal Book",
                        "author": "Jane Story",
                        "offlineFixtures": str(fixtures),
                        "audibleAuthPath": str(auth_path),
                        "requestDelay": 0,
                        "maxRequests": 3,
                    }
                )
        self.assertEqual(rc, 0)
        self.assertEqual(report["requestBudget"]["used"], 2)
        self.assertTrue(report["metadata"]["authenticatedPriceLookup"])
        self.assertEqual(report["results"][0]["status"], "discounted")
        self.assertEqual(report["results"][0]["pricing"]["discountPercent"], 75)
        self.assertEqual(report["results"][0]["pricing"]["priceBasis"], "audible_member_cash")
        self.assertEqual(report["results"][0]["pricing"]["dealType"], "member_cash_below_list")
        self.assertIn("authenticated Audible cash pricing enabled", markdown)
        self.assertIn("Cache:", markdown)
