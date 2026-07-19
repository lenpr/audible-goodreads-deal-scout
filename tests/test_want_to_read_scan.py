from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audible_goodreads_deal_scout import (  # noqa: E402
    goodreads_csv,
)
from audible_goodreads_deal_scout import audible_catalog  # noqa: E402
from audible_goodreads_deal_scout import goodreads_rating  # noqa: E402
from audible_goodreads_deal_scout import want_to_read_scan  # noqa: E402
from helpers import (  # noqa: E402
    GOODREADS_HEADERS,
    audible_search_card,
    scan_row,
    write_rows,
    write_want_to_read_fixtures,
)


class WantToReadScanTests(unittest.TestCase):
    def test_extract_to_read_entries_dedupes_and_ignores_extra_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            headers = GOODREADS_HEADERS + ["Irrelevant Future Column"]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                first = scan_row("10", "Deal Book", "Jane Story", "2026/04/03")
                first["Irrelevant Future Column"] = "ignored"
                duplicate = scan_row("10", "Deal Book", "Jane Story", "2026/04/04")
                duplicate["Irrelevant Future Column"] = "ignored"
                read_item = scan_row("11", "Read Book", "Jane Story", "2026/04/05", shelf="read")
                read_item["Irrelevant Future Column"] = "ignored"
                writer.writerows([first, duplicate, read_item])
            rows, stats = goodreads_csv.load_goodreads_csv(csv_path)
            entries = want_to_read_scan.extract_to_read_entries(rows)
        self.assertEqual(stats["totalRows"], 3)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rowKey"], "goodreads:10")

    def test_goodreads_csv_accepts_alternate_average_rating_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            headers = ["Community Rating" if header == "Average Rating" else header for header in GOODREADS_HEADERS]
            payload = scan_row("10", "Rated Book", "Jane Story", "2026/04/03")
            payload["Community Rating"] = "4.37"
            payload.pop("Average Rating")
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerow(payload)
            rows, stats = goodreads_csv.load_goodreads_csv(csv_path)
        self.assertEqual(stats["columnMap"]["average_rating"], "Community Rating")
        self.assertEqual(rows[0]["averageRating"], 4.37)
        self.assertEqual(rows[0]["averageRatingSource"], "csv_average_rating")

    def test_select_entries_supports_order_offset_limit_and_seed(self) -> None:
        entries = [
            {"rowKey": "a", "title": "A", "dateAdded": "2026-04-01"},
            {"rowKey": "b", "title": "B", "dateAdded": "2026-04-03"},
            {"rowKey": "c", "title": "C", "dateAdded": "2026-04-02"},
        ]
        newest = want_to_read_scan.select_entries(entries, scan_order="newest", seed="x", offset=1, limit=1)
        random_a = want_to_read_scan.select_entries(entries, scan_order="random", seed="stable", offset=0, limit=None)
        random_b = want_to_read_scan.select_entries(entries, scan_order="random", seed="stable", offset=0, limit=None)
        self.assertEqual([item["rowKey"] for item in newest], ["c"])
        self.assertEqual([item["rowKey"] for item in random_a], [item["rowKey"] for item in random_b])

    def test_want_to_read_scan_fixture_report_is_deterministic_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            write_rows(
                csv_path,
                [
                    scan_row("1", "Deal Book", "Jane Story", "2026/04/05"),
                    scan_row("2", "Hidden Book", "Jane Story", "2026/04/04"),
                    scan_row("3", "Unknown Book", "Jane Story", "2026/04/03"),
                    scan_row("4", "Second Card", "Jane Story", "2026/04/02"),
                    scan_row("5", "Missing Book", "Jane Story", "2026/04/01"),
                ],
            )
            config_path = tmp / "config.json"
            config_path.write_text(
                json.dumps({"audibleMarketplace": "us", "goodreadsCsvPath": str(csv_path), "artifactDir": str(tmp / "artifacts" / "current")}),
                encoding="utf-8",
            )
            deal_url = "https://www.audible.com/pd/Deal-Book-Audiobook/B000000001"
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001', 'Regular Price: $14.95 Sale Price: $4.99')}</ol>",
                    "Hidden Book Jane Story": f"<ol>{audible_search_card('Hidden Book', 'Jane Story', 'B000000002', 'Buy with 1 Credit. More Buying Choices')}</ol>",
                    "Unknown Book Jane Story": f"<ol>{audible_search_card('Unknown Book', 'Jane Story', 'B000000003')}</ol>",
                    "Second Card Jane Story": (
                        "<ol>"
                        + audible_search_card("Wrong Book", "Other Writer", "B000000004")
                        + audible_search_card("Second Card", "Jane Story", "B000000005")
                        + "</ol>"
                    ),
                    "Missing Book Jane Story": f"<ol>{audible_search_card('Unrelated Book', 'Other Writer', 'B000000006')}</ol>",
                },
                product={
                    deal_url: "<main><span>Regular Price: $14.95</span><span>Sale Price: $4.99</span></main>",
                },
            )
            report, markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "offlineFixtures": str(fixtures),
                    "requestDelay": 0,
                    "maxRequests": 20,
                }
            )
        self.assertEqual(rc, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["counts"]["totalWantToRead"], 5)
        self.assertEqual(report["counts"]["discounted"], 1)
        self.assertEqual(report["counts"]["priceHidden"], 1)
        self.assertEqual(report["counts"]["notFound"], 1)
        self.assertEqual(report["requestBudget"]["used"], 6)
        self.assertEqual(report["results"][0]["status"], "discounted")
        self.assertEqual(report["results"][0]["audible"]["title"], "Deal Book")
        self.assertEqual(report["results"][0]["pricing"]["dealType"], "limited_time_sale")
        self.assertIn("Deal Book", markdown)
        self.assertNotIn("Hidden Book", markdown)
        self.assertIn("Summary:", markdown)

    def test_want_to_read_markdown_includes_next_batch_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            write_rows(
                csv_path,
                [
                    scan_row("1", "Deal Book", "Jane Story", "2026/04/05"),
                    scan_row("2", "Later Book", "Jane Story", "2026/04/04"),
                ],
            )
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us", "goodreadsCsvPath": str(csv_path)}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001')}</ol>",
                    "Later Book Jane Story": f"<ol>{audible_search_card('Later Book', 'Jane Story', 'B000000002')}</ol>",
                },
            )
            report, markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "offlineFixtures": str(fixtures),
                    "requestDelay": 0,
                    "maxRequests": 5,
                    "limit": 1,
                }
            )
        self.assertEqual(rc, 0)
        self.assertEqual(report["counts"]["selectedRows"], 1)
        self.assertIn("Next batch:", markdown)
        self.assertIn("--offset 1 --limit 1", markdown)
        self.assertIn("anonymous Audible search/card pricing only", markdown)

    def test_want_to_read_scan_dedupes_repeated_audible_products(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            write_rows(
                csv_path,
                [
                    scan_row("1", "Deal Book", "Jane Story", "2026/04/05"),
                    scan_row("2", "Deal Book", "Jane Story", "2026/04/04"),
                ],
            )
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us", "goodreadsCsvPath": str(csv_path)}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            deal_url = "https://www.audible.com/pd/Deal-Book-Audiobook/B000000001"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001', 'Regular Price: $20.00 Sale Price: $5.00')}</ol>",
                },
                product={deal_url: "<main><span>Regular Price: $20.00</span><span>$5.00</span></main>"},
            )
            report, markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "offlineFixtures": str(fixtures),
                    "requestDelay": 0,
                    "maxRequests": 10,
                }
            )
        self.assertEqual(rc, 0)
        self.assertEqual(report["counts"]["scannedRows"], 2)
        self.assertEqual(report["counts"]["reportedResults"], 1)
        self.assertEqual(report["counts"]["duplicateAudibleProducts"], 1)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["deduplication"]["suppressedDuplicateCount"], 1)
        self.assertIn("1 duplicate Audible product rows suppressed", markdown)

    def test_want_to_read_scan_reports_json_progress_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us"}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Progress Book Jane Story": f"<ol>{audible_search_card('Progress Book', 'Jane Story', 'B000000009')}</ol>",
                },
            )
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                report, _markdown, rc = want_to_read_scan.scan_want_to_read(
                    {
                        "configPath": str(config_path),
                        "title": "Progress Book",
                        "author": "Jane Story",
                        "offlineFixtures": str(fixtures),
                        "requestDelay": 0,
                        "maxRequests": 5,
                        "progress": "json",
                        "progressInterval": 0,
                    }
                )
        self.assertEqual(rc, 0)
        self.assertEqual(report["status"], "completed")
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(any(event["event"] == "item" for event in events))
        self.assertEqual(events[-1]["scannedRows"], 1)

    def test_offer_parser_ignores_kindle_and_print_price_contexts(self) -> None:
        offer = audible_catalog.parse_offer_text(
            """
            <section>Kindle price: $1.99 Regular Price: $9.99</section>
            <section>Paperback List Price: $18.00</section>
            <section>Audible Regular Price: $14.95 Sale Price: $4.99</section>
            """
        )
        self.assertEqual(offer["currentPrice"], 4.99)
        self.assertEqual(offer["listPrice"], 14.95)
        self.assertEqual(offer["discountPercent"], 67)
        self.assertEqual(offer["priceBasis"], "audible_public_cash")
        self.assertEqual(offer["dealType"], "limited_time_sale")

    def test_goodreads_rating_parser_reads_json_ld_aggregate_rating(self) -> None:
        payload = goodreads_rating.parse_goodreads_rating(
            """
            <script type="application/ld+json">
            {"@type":"Book","aggregateRating":{"ratingValue":"4.42","ratingCount":"12,345"}}
            </script>
            """
        )
        self.assertEqual(payload["averageRating"], 4.42)
        self.assertEqual(payload["ratingsCount"], 12345)

    def test_want_to_read_scan_enriches_missing_goodreads_rating_for_discounted_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            item = scan_row("42", "Deal Book", "Jane Story", "2026/04/05")
            item["Average Rating"] = ""
            write_rows(csv_path, [item])
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us", "goodreadsCsvPath": str(csv_path)}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            deal_url = "https://www.audible.com/pd/Deal-Book-Audiobook/B000000001"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001', 'Regular Price: $14.95 Sale Price: $4.99')}</ol>",
                },
                product={
                    deal_url: "<main><span>Regular Price: $14.95</span><span>Sale Price: $4.99</span></main>",
                },
            )

            def rating_fetcher(_url: str) -> tuple[str, str]:
                return (
                    '<script type="application/ld+json">'
                    '{"@type":"Book","aggregateRating":{"ratingValue":"4.51","ratingCount":"999"}}'
                    "</script>",
                    "https://www.goodreads.com/book/show/42",
                )

            report, _markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "offlineFixtures": str(fixtures),
                    "enrichGoodreadsRatings": True,
                    "requestDelay": 0,
                    "maxRequests": 5,
                },
                goodreads_fetcher=rating_fetcher,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(report["results"][0]["goodreads"]["averageRating"], 4.51)
        self.assertEqual(report["results"][0]["goodreads"]["averageRatingSource"], "goodreads_public_page")
        self.assertEqual(report["goodreadsRatingEnrichment"]["updated"], 1)

    def test_budget_counts_product_fetch_separately_and_renders_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            csv_path = tmp / "goodreads.csv"
            write_rows(
                csv_path,
                [
                    scan_row("1", "Deal Book", "Jane Story", "2026/04/05"),
                    scan_row("2", "Later Book", "Jane Story", "2026/04/04"),
                ],
            )
            config_path = tmp / "config.json"
            config_path.write_text(json.dumps({"audibleMarketplace": "us", "goodreadsCsvPath": str(csv_path)}), encoding="utf-8")
            fixtures = tmp / "fixtures"
            write_want_to_read_fixtures(
                fixtures,
                search={
                    "Deal Book Jane Story": f"<ol>{audible_search_card('Deal Book', 'Jane Story', 'B000000001', 'Regular Price: $14.95 Sale Price: $4.99')}</ol>",
                    "Later Book Jane Story": f"<ol>{audible_search_card('Later Book', 'Jane Story', 'B000000002')}</ol>",
                },
                product={},
            )
            report, markdown, rc = want_to_read_scan.scan_want_to_read(
                {
                    "configPath": str(config_path),
                    "offlineFixtures": str(fixtures),
                    "requestDelay": 0,
                    "maxRequests": 1,
                }
            )
        self.assertEqual(rc, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["reasonCode"], "request_budget_exhausted")
        self.assertEqual(report["requestBudget"]["used"], 1)
        self.assertEqual(report["counts"]["scannedRows"], 1)
        self.assertIn("No visible numeric Audible discounts", markdown)
