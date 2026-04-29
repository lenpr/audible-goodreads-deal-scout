from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audible_goodreads_deal_scout import core, rendering  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def read_fixture_text(*parts: str) -> str:
    return (FIXTURE_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def read_fixture_json(*parts: str) -> dict[str, object]:
    return json.loads(read_fixture_text(*parts))


def marketplace_fetcher(marketplace: str):
    manifest = read_fixture_json("marketplaces", "certified_marketplaces.json")[marketplace]
    html_text = read_fixture_text("marketplaces", f"{marketplace}_dailydeal.html")

    def _fetch(_: str) -> tuple[str, str]:
        return html_text, str(manifest["finalUrl"])

    return _fetch


class PublishHardeningTests(unittest.TestCase):
    def test_readme_covers_goodreads_export_and_notes_only_setup(self) -> None:
        readme = Path(__file__).resolve().parents[1] / "README.md"
        content = readme.read_text(encoding="utf-8")
        self.assertIn("How to get your Goodreads CSV", content)
        self.assertIn("Open `My Books`", content)
        self.assertIn("Open `Import and Export`", content)
        self.assertIn("If you do not use Goodreads", content)
        self.assertIn("A strong notes file sounds like you talking to a smart bookseller", content)
        self.assertIn("TRUST.md", content)

    def test_skill_and_trust_docs_disclose_data_access_and_no_purchase_behavior(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        skill_text = (repo_root / "SKILL.md").read_text(encoding="utf-8")
        trust_text = (repo_root / "TRUST.md").read_text(encoding="utf-8")
        for content in (skill_text, trust_text):
            self.assertIn("does not buy", content)
            self.assertIn("Audible authentication is optional", content)
        self.assertIn("Trust And Data Access", skill_text)
        self.assertIn("What the skill may read", trust_text)
        self.assertIn("license: MIT-0", skill_text)

    def test_certified_marketplaces_manifest_matches_supported_marketplaces(self) -> None:
        manifest = read_fixture_json("marketplaces", "certified_marketplaces.json")
        self.assertEqual(sorted(manifest), sorted(core.SUPPORTED_MARKETPLACES))

    def test_certified_marketplace_fixtures_parse_expected_fields(self) -> None:
        manifest = read_fixture_json("marketplaces", "certified_marketplaces.json")
        for marketplace, expected in manifest.items():
            with self.subTest(marketplace=marketplace):
                candidate = core.parse_audible_deal(
                    read_fixture_text("marketplaces", f"{marketplace}_dailydeal.html"),
                    str(expected["finalUrl"]),
                    str(expected["dealUrl"]),
                )
                self.assertEqual(candidate["title"], expected["title"])
                self.assertEqual(candidate["author"], expected["author"])
                self.assertEqual(candidate["productId"], expected["productId"])
                self.assertEqual(candidate["audibleUrl"], expected["audibleUrl"])
                self.assertAlmostEqual(float(candidate["salePrice"]), float(expected["salePrice"]), places=2)
                self.assertAlmostEqual(float(candidate["listPrice"]), float(expected["listPrice"]), places=2)
                self.assertTrue(candidate["genres"])

    def test_certified_marketplace_price_display_matches_manifest(self) -> None:
        manifest = read_fixture_json("marketplaces", "certified_marketplaces.json")
        for marketplace, expected in manifest.items():
            with self.subTest(marketplace=marketplace):
                candidate = core.parse_audible_deal(
                    read_fixture_text("marketplaces", f"{marketplace}_dailydeal.html"),
                    str(expected["finalUrl"]),
                    str(expected["dealUrl"]),
                )
                self.assertEqual(rendering.price_display(candidate, marketplace), expected["priceDisplay"])

    def test_runtime_output_schema_matches_fixture(self) -> None:
        self.assertEqual(core.runtime_output_schema(), read_fixture_json("runtime", "runtime_output_schema.json"))

    def test_validate_runtime_output_rejects_semantically_invalid_payloads(self) -> None:
        invalid_payloads = [
            (
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                        "averageRating": 4.2,
                    },
                    "fit": {"status": "written", "sentence": "Fit: Strong match."},
                },
                "Resolved Goodreads output must include: url.",
            ),
            (
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "url": "https://www.goodreads.com/book/show/1",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                    },
                    "fit": {"status": "written", "sentence": "Fit: Strong match."},
                },
                "Resolved Goodreads output must include: averageRating.",
            ),
            (
                {
                    "schemaVersion": 1,
                    "goodreads": {"status": "no_match", "averageRating": 4.2},
                    "fit": {"status": "not_applicable"},
                },
                "Goodreads status 'no_match' must not include averageRating.",
            ),
            (
                {
                    "schemaVersion": 1,
                    "goodreads": {"status": "lookup_failed", "url": "https://www.goodreads.com/book/show/1"},
                    "fit": {"status": "unavailable"},
                },
                "Goodreads status 'lookup_failed' must not include url.",
            ),
            (
                {
                    "schemaVersion": 1,
                    "goodreads": {
                        "status": "resolved",
                        "url": "https://www.goodreads.com/book/show/1",
                        "title": "Signal Fire",
                        "author": "Jane Story",
                        "averageRating": 4.2,
                    },
                    "fit": {"status": "written", "sentence": "   "},
                },
                "fit.status 'written' requires a non-empty sentence.",
            ),
        ]
        for payload, expected_message in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, expected_message):
                    core.validate_runtime_output(payload)

    def test_validate_runtime_output_normalizes_non_written_fit_sentence_to_none(self) -> None:
        payload = core.validate_runtime_output(
            {
                "schemaVersion": 1,
                "goodreads": {"status": "lookup_failed"},
                "fit": {"status": "unavailable", "sentence": "ignored"},
            }
        )
        self.assertIsNone(payload["fit"]["sentence"])

    def test_runtime_prompt_includes_review_summary_and_privacy_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            prep = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "notesText": "I like speculative fiction with political bite.",
                },
                fetcher=marketplace_fetcher("us"),
            )
            prompt_text = Path(prep["artifacts"]["runtimePromptPath"]).read_text(encoding="utf-8")
        self.assertIn("summarize each review-bearing entry to 500 characters or fewer", prompt_text)
        self.assertIn("If privacyMode is minimal, do not use personal CSV or notes content.", prompt_text)
        self.assertIn('"schemaVersion": 1', prompt_text)

    def test_runtime_prompt_mentions_to_read_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            csv_path.write_text(
                "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,Average Rating,Publisher,Binding,Number of Pages,Year Published,Original Publication Year,Date Read,Date Added,Bookshelves,Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,Private Notes,Read Count,Owned Copies\n"
                "1,Signal Fire,Jane Story,,,,5,4.2,,,,,2022,2022,,2026-04-01,,,to-read,,,,,\n",
                encoding="utf-8",
            )
            prep = core.prepare_run(
                {
                    "artifactDir": str(tmp / "artifacts"),
                    "audibleMarketplace": "us",
                    "goodreadsCsvPath": str(csv_path),
                },
                fetcher=marketplace_fetcher("us"),
            )
            prompt_text = Path(prep["artifacts"]["runtimePromptPath"]).read_text(encoding="utf-8")
        self.assertIn("This book is already on the user's Goodreads to-read shelf.", prompt_text)

    def test_minimal_privacy_message_uses_generic_fit(self) -> None:
        final = core.finalize_skill_result(
            {
                "schemaVersion": 1,
                "status": "ready",
                "reasonCode": "ready_notes",
                "warnings": [],
                "audible": {
                    "title": "Glass Harbor",
                    "author": "Nora Vale",
                    "year": 2023,
                    "salePrice": 2.99,
                    "listPrice": 12.99,
                    "runtime": "9 hrs and 05 mins",
                    "genres": ["Mystery", "Psychological"],
                    "summary": "A moody coastal mystery with a knotty family secret.",
                    "audibleUrl": "https://www.audible.co.uk/pd/Glass-Harbor-Audiobook/UKC1234567",
                },
                "personalData": {"mode": "notes", "privacyMode": "minimal", "exactShelfMatch": ""},
                "artifacts": {},
                "metadata": {
                    "marketplace": "uk",
                    "marketplaceLabel": "Audible UK",
                    "storeLocalDate": "2026-04-20",
                    "threshold": 3.8,
                },
            },
            {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/2",
                    "title": "Glass Harbor",
                    "author": "Nora Vale",
                    "averageRating": 4.05,
                    "ratingsCount": 18750,
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertIn(core.FIT_NO_PERSONAL_DATA, final["message"])
        self.assertIn("Audible UK Daily Promotion — 2026-04-20", final["message"])
        self.assertIn("Price: £2.99 (-77%, list price £12.99)", final["message"])

    def test_warning_block_renders_after_links(self) -> None:
        message = rendering.render_final_message(
            {
                "status": "recommend",
                "reasonCode": "recommend_public_threshold",
                "fitSentence": core.FIT_NO_PERSONAL_DATA,
                "warnings": ["Your Goodreads export is 190 days old."],
                "audible": {
                    "title": "Storm Atlas",
                    "author": "Clare Holden",
                    "year": 2024,
                    "salePrice": 4.49,
                    "listPrice": 16.99,
                    "runtime": "12 hrs and 02 mins",
                    "genres": ["Fantasy", "Epic"],
                    "summary": "An expansive fantasy adventure with weather magic and political intrigue.",
                    "audibleUrl": "https://www.audible.com.au/pd/Storm-Atlas-Audiobook/AUS1234567",
                },
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/3",
                    "averageRating": 4.01,
                    "ratingsCount": 22001,
                },
                "metadata": {
                    "marketplace": "au",
                    "marketplaceLabel": "Audible AU",
                    "storeLocalDate": "2026-04-20",
                },
            }
        )
        self.assertLess(message.index("Audible: https://www.audible.com.au/pd/Storm-Atlas-Audiobook/AUS1234567"), message.index("Warnings: Your Goodreads export is 190 days old."))
        self.assertIn("Goodreads: https://www.goodreads.com/book/show/3", message)

    def test_cross_market_rendering_uses_localized_headers_and_currency(self) -> None:
        final = core.finalize_skill_result(
            {
                "schemaVersion": 1,
                "status": "ready",
                "reasonCode": "ready_public",
                "warnings": [],
                "audible": {
                    "title": "Die Stadt aus Glas",
                    "author": "Mira Falk",
                    "year": 2021,
                    "salePrice": 4.99,
                    "listPrice": 19.95,
                    "runtime": "10 hrs and 07 mins",
                    "genres": ["Science Fiction", "Spannung"],
                    "summary": "Ein futuristischer Thriller über Erinnerung, Kontrolle und Verrat.",
                    "audibleUrl": "https://www.audible.de/pd/Die-Stadt-aus-Glas-Hoerbuch/DE12345678",
                },
                "personalData": {"mode": "public", "privacyMode": "normal", "exactShelfMatch": ""},
                "artifacts": {},
                "metadata": {
                    "marketplace": "de",
                    "marketplaceLabel": "Audible DE",
                    "storeLocalDate": "2026-04-20",
                    "threshold": 3.8,
                },
            },
            {
                "schemaVersion": 1,
                "goodreads": {
                    "status": "resolved",
                    "url": "https://www.goodreads.com/book/show/4",
                    "title": "Die Stadt aus Glas",
                    "author": "Mira Falk",
                    "averageRating": 4.22,
                    "ratingsCount": 8471,
                },
                "fit": {"status": "not_applicable"},
            },
        )
        self.assertIn("Audible DE Daily Promotion — 2026-04-20", final["message"])
        self.assertIn("Price: €4.99 (-75%, list price €19.95)", final["message"])
        self.assertIn("Length: 10:07 hrs", final["message"])


if __name__ == "__main__":
    unittest.main()
