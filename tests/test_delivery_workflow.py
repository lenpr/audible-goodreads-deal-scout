from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import (  # noqa: E402
    constants,
    core,
    public_cli,
    settings,
    shared,
)
from audible_goodreads_deal_scout import delivery as delivery_mod  # noqa: E402
from audible_goodreads_deal_scout import repo_audit  # noqa: E402
from audible_goodreads_deal_scout import rendering  # noqa: E402
from helpers import (  # noqa: E402
    PERSONALIZED_FIT,
    fake_fetcher,
    read_message_fixture,
    row,
    write_rows,
)


class DeliveryWorkflowTests(unittest.TestCase):
    def test_setup_writes_config_and_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir).resolve()
            result = delivery_mod.setup_configuration(
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
            next_steps = {step["label"]: step for step in result["nextSteps"]}
            self.assertIn("doctor", next_steps)
            self.assertIn("check-daily-deal", next_steps)
            self.assertIn("scan-want-to-read", next_steps)
            self.assertIn("optional-audible-auth", next_steps)
            self.assertIn(str(tmp / "config.json"), next_steps["doctor"]["command"])
            self.assertEqual(next_steps["scan-want-to-read"]["argv"][-2:], ["--limit", "40"])

    def test_setup_returns_manual_instructions_when_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir).resolve()
            with mock.patch.object(delivery_mod, "write_json_atomic", side_effect=OSError("denied")):
                result = delivery_mod.setup_configuration({"storageDir": str(tmp), "audibleMarketplace": "us"})
        self.assertFalse(result["written"])
        self.assertTrue(result["manualOnly"])
        self.assertIn('"audibleMarketplace": "us"', result["configJson"])

    def test_setup_cron_registration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir).resolve()
            config_path = tmp / "config.json"
            state_path = tmp / "state.json"
            spec = settings.validate_marketplace("us")
            expected_message = delivery_mod.build_cron_message(config_path, state_path)
            existing = {
                "id": "job-1",
                "name": "Audible Goodreads Deal (US)",
                "schedule": {"cron": spec["defaultCron"], "tz": spec["timezone"]},
                "payload": {"message": expected_message, "lightContext": True, "thinking": "off"},
                "trigger": {"script": "json({ fire: true });"},
            }
            with mock.patch.object(delivery_mod, "list_cron_jobs", return_value=[existing]):
                result = delivery_mod.setup_configuration(
                    {"storageDir": str(tmp), "audibleMarketplace": "us", "dailyAutomation": True},
                    register_cron=True,
                )
        registration = result["cronRegistration"]
        self.assertTrue(registration["ok"])
        self.assertFalse(registration["created"])
        self.assertEqual(registration["existingJob"]["id"], "job-1")

    def test_cron_command_uses_configured_delivery_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            state_path = tmp / "state.json"
            shared.write_json_atomic(
                config_path,
                {"deliveryChannel": "telegram", "deliveryTarget": "-1000000000000"},
            )
            command = delivery_mod.build_cron_command(
                openclaw_bin="/fake/openclaw",
                spec=settings.validate_marketplace("us"),
                config_path=config_path,
                state_file=state_path,
            )
        self.assertIn("--channel", command)
        self.assertIn("telegram", command)
        self.assertIn("--to", command)
        self.assertIn("-1000000000000", command)

    def test_register_cron_reconciles_related_job_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            state_path = tmp / "state.json"
            shared.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1000000000000",
                },
            )
            message = (
                f"Use $audible-goodreads-deal-scout with config at {config_path} "
                "in scheduled mode."
            )
            related = {
                "id": "job-drifted",
                "name": "Daily Audible deal watch (Books)",
                "enabled": True,
                "schedule": {"expr": "0 12 * * *", "tz": "Europe/Lisbon"},
                "delivery": {"mode": "announce", "channel": "telegram", "to": "-5038675285"},
                "payload": {"message": message},
            }
            updated = {
                "id": "job-drifted",
                "name": "Daily Audible deal watch (Books)",
                "enabled": True,
                "schedule": {"expr": "0 12 * * *", "tz": "America/Los_Angeles"},
                "delivery": {"mode": "announce", "channel": "telegram", "to": "-1000000000000"},
                "payload": {
                    "message": delivery_mod.build_cron_message(config_path, state_path),
                    "lightContext": True,
                    "thinking": "off",
                },
                "trigger": {"script": "json({ fire: true });"},
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"job": {"id": "job-drifted", "enabled": True}}),
                stderr="",
            )
            with (
                mock.patch.object(delivery_mod, "list_cron_jobs", side_effect=[[related], [updated]]),
                mock.patch.object(delivery_mod.subprocess, "run", return_value=completed) as patched,
            ):
                result = delivery_mod.register_cron_job(
                    openclaw_bin="/fake/openclaw",
                    spec=settings.validate_marketplace("us"),
                    config_path=config_path,
                    state_file=state_path,
                    name="Daily Audible deal watch (Books)",
                    cron_expr="0 12 * * *",
                )
        command = patched.call_args.args[0]
        self.assertTrue(result["updated"])
        self.assertFalse(result["created"])
        self.assertEqual(command[3:5], ["edit", "job-drifted"])
        self.assertIn("America/Los_Angeles", command)
        self.assertIn("-1000000000000", command)

    def test_setup_preserves_existing_config_when_registering_cron(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            csv_path = tmp / "goodreads.csv"
            auth_path = tmp / "audible-auth.json"
            csv_path.write_text("Book Id,Title\n", encoding="utf-8")
            auth_path.write_text("{}", encoding="utf-8")
            shared.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(csv_path),
                    "audibleAuthPath": str(auth_path),
                    "privacyMode": "normal",
                    "dailyCron": "0 12 * * *",
                    "stateFile": str(tmp / "state.json"),
                    "artifactDir": str(tmp / "artifacts" / "current"),
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1000000000000",
                    "deliveryPolicy": "positive_only",
                    "csvColumns": {"title": "Title"},
                },
            )
            result = delivery_mod.setup_configuration(
                {"configPath": str(config_path), "dailyAutomation": True},
                register_cron=False,
            )
            restored = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(result["written"])
        self.assertEqual(restored["goodreadsCsvPath"], str(csv_path))
        self.assertEqual(restored["audibleAuthPath"], str(auth_path))
        self.assertEqual(restored["deliveryChannel"], "telegram")
        self.assertEqual(restored["deliveryTarget"], "-1000000000000")
        self.assertEqual(restored["dailyCron"], "0 12 * * *")
        self.assertEqual(restored["csvColumns"], {"title": "Title"})

    def test_resolve_openclaw_bin_uses_common_user_install_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fallback = tmp / ".npm-global" / "bin" / "openclaw"
            fallback.parent.mkdir(parents=True)
            fallback.write_text("#!/bin/sh\n", encoding="utf-8")
            if os.name == "posix":
                os.chmod(fallback, 0o755)
            with (
                mock.patch.object(delivery_mod.Path, "home", return_value=tmp),
                mock.patch.object(delivery_mod.shutil, "which", return_value=None),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                resolved = delivery_mod.resolve_openclaw_bin("openclaw")
        self.assertEqual(resolved, str(fallback))

    def test_resolve_delivery_settings_prefers_explicit_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            shared.write_json_atomic(
                config_path,
                {
                    "audibleMarketplace": "us",
                    "deliveryChannel": "telegram",
                    "deliveryTarget": "-1",
                },
            )
            resolved_path, channel, target, policy = delivery_mod.resolve_delivery_settings(
                config_path=config_path,
                delivery_channel="telegram",
                delivery_target="-2",
            )
        self.assertEqual(resolved_path, config_path.resolve())
        self.assertEqual(channel, "telegram")
        self.assertEqual(target, "-2")
        self.assertEqual(policy, constants.DEFAULT_DELIVERY_POLICY)

    def test_deliver_message_uses_openclaw_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            shared.write_json_atomic(
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
                result = delivery_mod.deliver_message(
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
        self.assertEqual(result["deliveryPolicy"], constants.DEFAULT_DELIVERY_POLICY)

    def test_publish_audit_reports_skill_key_and_publish_command(self) -> None:
        args = mock.Mock(version=public_cli.__version__, tags="latest,stable")
        with mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_publish_audit(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        payload = json.loads(output_text)
        self.assertTrue(payload["files"]["LICENSE.txt"])
        self.assertTrue(payload["files"]["TRUST.md"])
        self.assertTrue(payload["files"]["scripts/audible-goodreads-deal-scout.sh"])
        self.assertTrue(payload["frontmatter"]["hasLicense"])
        self.assertTrue(payload["frontmatter"]["hasSkillKey"])
        self.assertTrue(payload["frontmatter"]["hasCategory"])
        self.assertTrue(payload["publishIgnore"]["exists"])
        self.assertTrue(payload["publishIgnore"]["requiredExclusionsPresent"])
        self.assertEqual(payload["publishIgnore"]["missingExclusions"], [])
        required_exclusions = set(payload["publishIgnore"]["requiredExclusions"])
        self.assertIn("audible-auth*.json", required_exclusions)
        self.assertIn(".DS_Store", required_exclusions)
        self.assertIn(".git/", required_exclusions)
        self.assertTrue(payload["privacyAudit"]["ok"])
        self.assertIn("clawhub publish", payload["recommendedPublishCommand"])
        self.assertTrue(payload["recommendedPublishCommand"].startswith("clawhub publish . "))

    def test_version_command_reports_package_version(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = public_cli.main(["version"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["version"], public_cli.__version__)

    def test_repo_audit_detects_private_machine_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            leak_text = "private file: /Users/private-user/work/config.json"
            (tmp / "notes.txt").write_text(leak_text, encoding="utf-8")
            payload = repo_audit.scan_repo_for_leaks(tmp)
        self.assertFalse(payload["ok"])
        markers = {finding["marker"] for finding in payload["findings"]}
        self.assertIn("absolute_home_path", markers)

    def test_bold_visible_text_styles_ascii_title(self) -> None:
        self.assertEqual(rendering.bold_visible_text("Signal Fire"), "𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲")

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
        plan = rendering.build_delivery_plan(final_result, "positive_only")
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
        plan = rendering.build_delivery_plan(final_result, "summary_on_non_match")
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
        plan = rendering.build_delivery_plan(final_result, "summary_on_non_match")
        self.assertTrue(plan["shouldDeliver"])
        self.assertIn("Fit: Goodreads could not be verified right now.", plan["message"])

    def test_message_snapshots_match_expected_layout(self) -> None:
        prep = core.prepare_run({"audibleMarketplace": "us", "today": "2026-04-20"}, fetcher=fake_fetcher)
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
                    "ratingsCount": 9501,
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
                    "today": "2026-04-20",
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
                        "sentence": PERSONALIZED_FIT,
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
            delivered = {"ok": True, "delivered": True, "payload": {"ok": True, "messageId": "7"}}
            args = mock.Mock(
                prepare_json=str(prepare_path),
                runtime_output=str(runtime_path),
                config_path=str(tmp / "config.json"),
                delivery_channel=None,
                delivery_target=None,
                delivery_policy="positive_only",
                openclaw_bin="openclaw",
                dry_run=False,
            )
            with mock.patch.object(public_cli, "deliver_message", return_value=delivered), mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
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
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(public_cli, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "positive_only")), mock.patch.object(public_cli, "deliver_message") as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
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
        delivered = {"ok": True, "delivered": True, "payload": {"ok": True, "messageId": "8"}}
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
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(public_cli, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "summary_on_non_match")), mock.patch.object(public_cli, "deliver_message", return_value=delivered) as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        self.assertIn("Fit: You marked it as read on Goodreads.", deliver_mock.call_args.kwargs["message_text"])
        self.assertIn("Audible US Daily Promotion — 2026-04-20", deliver_mock.call_args.kwargs["message_text"])
        payload = json.loads(output_text)
        self.assertTrue(payload["delivered"])
        self.assertEqual(payload["deliveryPlan"]["mode"], "summary")

    def test_run_and_deliver_refuses_scheduled_error_prepare_result(self) -> None:
        prepare = {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_audible_fetch_failed",
            "message": "Audible fetch failed.",
            "warnings": [],
            "audible": {},
            "personalData": {},
            "artifacts": {},
            "metadata": {
                "marketplace": "us",
                "marketplaceLabel": "Audible US",
                "storeLocalDate": "2026-04-20",
                "invocationMode": "scheduled",
            },
        }
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
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(public_cli, "deliver_message") as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)

        self.assertEqual(rc, 1)
        deliver_mock.assert_not_called()
        payload = json.loads(output_text)
        self.assertFalse(payload["delivered"])
        self.assertEqual(payload["reasonCode"], "error_scheduled_prepare_failed")

    def test_run_and_deliver_refuses_stale_scheduled_prepare_result(self) -> None:
        prepare = {
            "schemaVersion": 1,
            "status": "ready",
            "reasonCode": "ready_public",
            "message": "Ready.",
            "warnings": [],
            "audible": {"title": "Signal Fire", "author": "Jane Story"},
            "personalData": {"mode": "public", "privacyMode": "normal"},
            "artifacts": {},
            "metadata": {
                "marketplace": "us",
                "marketplaceLabel": "Audible US",
                "storeLocalDate": "2026-04-19",
                "invocationMode": "scheduled",
            },
        }
        args = mock.Mock(
            prepare_json="-",
            runtime_output=None,
            config_path=None,
            delivery_channel=None,
            delivery_target=None,
            delivery_policy="always_full",
            openclaw_bin="openclaw",
            dry_run=False,
        )
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(core, "logical_store_date", return_value=date(2026, 4, 20)), mock.patch.object(public_cli, "deliver_message") as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)

        self.assertEqual(rc, 1)
        deliver_mock.assert_not_called()
        payload = json.loads(output_text)
        self.assertEqual(payload["reasonCode"], "error_stale_scheduled_prepare_result")
        self.assertIn("2026-04-19", payload["error"])

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
        with mock.patch.object(public_cli, "load_json_input", side_effect=[prepare]), mock.patch.object(public_cli, "resolve_delivery_policy", return_value=(Path("/tmp/config.json"), "always_full")), mock.patch.object(public_cli, "deliver_message", side_effect=RuntimeError("send failed")) as deliver_mock, mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
            rc = public_cli.command_run_and_deliver(args)
            output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
        self.assertEqual(rc, 1)
        deliver_mock.assert_called_once()
        payload = json.loads(output_text)
        self.assertFalse(payload["ok"])
        self.assertIn("send failed", payload["error"])

    def test_mark_emitted_uses_current_scheduled_prepare_artifact_deal_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state_file = tmp / "state.json"
            prepare_path = tmp / "prepare-result.json"
            prepare_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "reasonCode": "ready_public",
                        "warnings": [],
                        "audible": {"title": "Signal Fire", "author": "Jane Story"},
                        "personalData": {},
                        "artifacts": {"prepareResultPath": str(prepare_path)},
                        "metadata": {
                            "marketplace": "us",
                            "storeLocalDate": "2026-04-20",
                            "invocationMode": "scheduled",
                            "stateFile": str(state_file),
                            "dealKey": "us:2026-04-20:ABC1234567",
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = mock.Mock(
                state_file=str(state_file),
                prepare_json=str(prepare_path),
                deal_key="us:2026-04-20:ABC1234567",
                stale_warning_date=None,
            )
            with mock.patch.object(core, "logical_store_date", return_value=date(2026, 4, 20)), mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
                rc = public_cli.command_mark_emitted(args)
                output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)
            state = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(state["lastEmittedDealKey"], "us:2026-04-20:ABC1234567")
        self.assertEqual(json.loads(output_text)["dealKey"], "us:2026-04-20:ABC1234567")

    def test_mark_emitted_rejects_mismatched_deal_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            state_file = tmp / "state.json"
            prepare_path = tmp / "prepare-result.json"
            prepare_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "reasonCode": "ready_public",
                        "warnings": [],
                        "audible": {"title": "Signal Fire", "author": "Jane Story"},
                        "personalData": {},
                        "artifacts": {"prepareResultPath": str(prepare_path)},
                        "metadata": {
                            "marketplace": "us",
                            "storeLocalDate": "2026-04-20",
                            "invocationMode": "scheduled",
                            "stateFile": str(state_file),
                            "dealKey": "us:2026-04-20:ABC1234567",
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = mock.Mock(
                state_file=str(state_file),
                prepare_json=str(prepare_path),
                deal_key="us:2026-04-27:STALE",
                stale_warning_date=None,
            )
            with mock.patch.object(core, "logical_store_date", return_value=date(2026, 4, 20)), mock.patch("sys.stdout", new_callable=mock.MagicMock()) as fake_stdout:
                rc = public_cli.command_mark_emitted(args)
                output_text = "".join(call.args[0] for call in fake_stdout.write.call_args_list)

        self.assertEqual(rc, 1)
        self.assertFalse(state_file.exists())
        self.assertIn("refused deal key", json.loads(output_text)["error"])
