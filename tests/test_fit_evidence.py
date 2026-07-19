from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import core, fit_evidence  # noqa: E402
from helpers import fake_fetcher, row, write_rows  # noqa: E402


class FitEvidenceTests(unittest.TestCase):
    def test_selector_prioritizes_related_books_and_balances_review_anchors(self) -> None:
        candidate = {
            "title": "Signal Fire",
            "author": "Jane Story",
            "genres": ["Science Fiction", "Thriller"],
            "summary": "A political science fiction thriller about institutional pressure and family loyalty.",
        }
        rows = [
            {
                "title": "A Different Signal",
                "author": "Jane Story",
                "myRating": 4,
                "myReview": "Its political tension made the family choices feel consequential.",
                "exclusiveShelf": "read",
            },
            {
                "title": "Warm Harbor",
                "author": "Other Writer",
                "myRating": 5,
                "myReview": "I loved the intimate character work and generous emotional voice.",
                "exclusiveShelf": "read",
            },
            {
                "title": "Cold Framework",
                "author": "Third Writer",
                "myRating": 2,
                "myReview": "The ideas were interesting, but the distant characters never became convincing.",
                "exclusiveShelf": "read",
            },
        ]
        rows.extend(
            {
                "title": f"Unrelated Book {index}",
                "author": f"Writer {index}",
                "myRating": 4,
                "myReview": "",
                "exclusiveShelf": "read",
            }
            for index in range(20)
        )
        rows[1]["myReview"] = rows[1]["myReview"] + " Detailed evidence." * 300

        evidence = fit_evidence.build_fit_evidence(candidate, rows)
        entries = evidence["entries"]
        self.assertLessEqual(len(entries), fit_evidence.MAX_FIT_EVIDENCE_ENTRIES)
        self.assertEqual(entries[0]["title"], "A Different Signal")
        self.assertIn("same_author", entries[0]["selectionReasons"])
        self.assertTrue(any("positive_review_anchor" in item["selectionReasons"] for item in entries))
        self.assertTrue(any("critical_review_anchor" in item["selectionReasons"] for item in entries))
        self.assertTrue(all(item["evidenceStrength"] in {"review_backed", "rating_only"} for item in entries))
        longest_review = max(len(str(item.get("reviewText") or "")) for item in entries)
        self.assertLessEqual(longest_review, fit_evidence.MAX_FIT_EVIDENCE_REVIEW_CHARS)

    def test_prepare_writes_bounded_fit_evidence_and_specific_prompt_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            rows = [
                row(
                    title="A Different Signal",
                    author="Jane Story",
                    shelf="read",
                    rating="4",
                    review="The political tension made the family choices feel consequential.",
                ),
                row(
                    title="Warm Harbor",
                    author="Other Writer",
                    shelf="read",
                    rating="5",
                    review="I loved its intimate character work and emotional generosity.",
                ),
                row(
                    title="Cold Framework",
                    author="Third Writer",
                    shelf="read",
                    rating="2",
                    review="The distant characters undermined the interesting ideas.",
                ),
            ]
            rows.extend(
                row(title=f"Unrelated Book {index}", author=f"Writer {index}", shelf="read", rating="4")
                for index in range(20)
            )
            write_rows(csv_path, rows)
            result = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(csv_path),
                },
                fetcher=fake_fetcher,
            )
            evidence = json.loads(Path(result["artifacts"]["fitEvidencePath"]).read_text(encoding="utf-8"))
            prompt = Path(result["artifacts"]["runtimePromptPath"]).read_text(encoding="utf-8")

        self.assertEqual(evidence["selection"]["totalEligibleEntries"], 23)
        self.assertLess(evidence["selection"]["selectedEntryCount"], 23)
        self.assertIn("Read artifacts.fitEvidencePath first", prompt)
        self.assertIn("do not open with 'strong fit'", prompt)
        self.assertIn("A rating_only entry supports only", prompt)


if __name__ == "__main__":
    unittest.main()
