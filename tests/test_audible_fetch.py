from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import audible_catalog, audible_fetch, core  # noqa: E402
from helpers import AUDIBLE_HTML, FakeHttpResponse  # noqa: E402


class AudibleFetchTests(unittest.TestCase):
    def test_daily_promotion_fetch_uses_browser_like_headers(self) -> None:
        seen_headers: dict[str, str] = {}

        def fake_urlopen(request: object, timeout: int = 30) -> FakeHttpResponse:
            del timeout
            seen_headers.update({key.lower(): value for key, value in request.header_items()})  # type: ignore[attr-defined]
            return FakeHttpResponse(AUDIBLE_HTML, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567")

        with mock.patch.object(audible_fetch.urllib.request, "urlopen", side_effect=fake_urlopen):
            text, final_url = audible_fetch.fetch_text_with_final_url("https://www.audible.com/dailydeal", retries=0)

        self.assertIn("Mozilla/5.0", seen_headers["user-agent"])
        self.assertEqual(seen_headers["accept-language"], "en-US,en;q=0.9")
        self.assertIn("Signal Fire", text)
        self.assertEqual(final_url, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567")

    def test_audible_fetch_rejects_non_audible_urls(self) -> None:
        with mock.patch.object(audible_fetch.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(audible_fetch.AudibleFetchError) as context:
                audible_fetch.fetch_text_with_final_url("https://example.com/dailydeal", retries=0)
        self.assertFalse(urlopen.called)
        self.assertEqual(context.exception.reason_code, "error_unsafe_audible_url")

    def test_catalog_fetch_rejects_unsupported_audible_paths(self) -> None:
        with mock.patch.object(audible_catalog.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(audible_fetch.AudibleFetchError) as context:
                audible_catalog.fetch_catalog_text_with_final_url("https://www.audible.com/account")
        self.assertFalse(urlopen.called)
        self.assertEqual(context.exception.reason_code, "error_unsupported_audible_path")

    def test_daily_promotion_fetch_recovers_with_curl_after_python_503(self) -> None:
        def failing_urlopen(request: object, timeout: int = 30) -> FakeHttpResponse:
            del timeout
            raise audible_fetch.HTTPError(
                request.full_url,  # type: ignore[attr-defined]
                503,
                "Service Unavailable",
                hdrs=None,
                fp=None,
            )

        curl_stdout = (
            AUDIBLE_HTML
            + "\n"
            + audible_fetch.CURL_META_MARKER
            + "200\thttps://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"
        )
        completed = subprocess.CompletedProcess(["curl"], 0, stdout=curl_stdout, stderr="")

        with (
            mock.patch.object(audible_fetch.urllib.request, "urlopen", side_effect=failing_urlopen),
            mock.patch.object(audible_fetch, "curl_available", return_value=True),
            mock.patch.object(audible_fetch.subprocess, "run", return_value=completed),
        ):
            result = audible_fetch.fetch_text_with_final_url(
                "https://www.audible.com/dailydeal",
                retries=0,
                backend="auto",
            )

        text, final_url = result
        self.assertIn("Signal Fire", text)
        self.assertEqual(final_url, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567")
        self.assertEqual(result.backend, "curl")
        self.assertEqual(result.attempts[0]["reasonCode"], "http_503_python_fetch_rejected")
        self.assertEqual(result.attempts[-1]["backend"], "curl")
        self.assertTrue(any("recovered with curl fallback" in warning for warning in result.warnings))

    def test_prepare_records_curl_fallback_metadata_after_python_503(self) -> None:
        def failing_urlopen(request: object, timeout: int = 30) -> FakeHttpResponse:
            del timeout
            raise audible_fetch.HTTPError(
                request.full_url,  # type: ignore[attr-defined]
                503,
                "Service Unavailable",
                hdrs=None,
                fp=None,
            )

        curl_stdout = (
            AUDIBLE_HTML
            + "\n"
            + audible_fetch.CURL_META_MARKER
            + "200\thttps://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"
        )
        completed = subprocess.CompletedProcess(["curl"], 0, stdout=curl_stdout, stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            artifact_dir = Path(tmp_dir) / "artifacts"
            with (
                mock.patch.object(audible_fetch.urllib.request, "urlopen", side_effect=failing_urlopen),
                mock.patch.object(audible_fetch, "curl_available", return_value=True),
                mock.patch.object(audible_fetch.subprocess, "run", return_value=completed),
            ):
                result = core.prepare_run(
                    {
                        "artifactDir": str(artifact_dir),
                        "audibleMarketplace": "us",
                        "audibleFetchBackend": "auto",
                        "audibleFetchRetries": 0,
                    }
                )

        fetch_metadata = result["metadata"]["fetch"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["audible"]["title"], "Signal Fire")
        self.assertEqual(fetch_metadata["backend"], "curl")
        self.assertTrue(fetch_metadata["recoveredByFallback"])
        self.assertEqual(fetch_metadata["firstFailureReasonCode"], "http_503_python_fetch_rejected")
        self.assertTrue(any("recovered with curl fallback" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
