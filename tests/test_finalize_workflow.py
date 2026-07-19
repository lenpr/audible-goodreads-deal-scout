from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import (  # noqa: E402
    core,
)
from helpers import (  # noqa: E402
    PERSONALIZED_FIT,
    fake_fetcher,
    row,
    write_rows,
)


class FinalizeWorkflowTests(unittest.TestCase):
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
                        "sentence": PERSONALIZED_FIT,
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
        self.assertIn("It is already on your Goodreads to-read shelf", final["message"])
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
                    "ratingsCount": 9501,
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertEqual(final["status"], "recommend")
        self.assertEqual(final["reasonCode"], "recommend_public_threshold")
        self.assertIn("Goodreads rating: 4.15 (9,501 ratings)", final["message"])
        self.assertIn("𝗦𝗶𝗴𝗻𝗮𝗹 𝗙𝗶𝗿𝗲 — Jane Story (2022)", final["message"])

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
