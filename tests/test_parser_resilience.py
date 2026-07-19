from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audible_goodreads_deal_scout import audible_catalog, audible_source, goodreads_rating  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parser_variants"


class ParserResilienceTests(unittest.TestCase):
    def test_audible_deal_accepts_attribute_order_quotes_graph_and_nested_text(self) -> None:
        candidate = audible_source.parse_audible_deal(
            (FIXTURES / "audible_attribute_variants.html").read_text(encoding="utf-8"),
            "https://www.audible.com/pd/Signal-Fire-Audiobook/abc1234567?source=dailydeal",
            "https://www.audible.com/dailydeal",
        )
        self.assertEqual(candidate["title"], "Signal Fire: A Variant")
        self.assertEqual(candidate["author"], "Jane Story")
        self.assertEqual(candidate["productId"], "ABC1234567")
        self.assertEqual(candidate["summary"], "A resilient parser should preserve this nested summary.")
        self.assertEqual(candidate["coverUrl"], "https://example.com/variant-cover.jpg")
        self.assertEqual(candidate["genres"], ["Science Fiction", "Political Thriller"])
        self.assertEqual(candidate["salePrice"], 5.99)
        self.assertEqual(candidate["listPrice"], 18.95)

    def test_goodreads_rating_accepts_graph_and_skips_malformed_json_ld(self) -> None:
        payload = goodreads_rating.parse_goodreads_rating(
            (FIXTURES / "goodreads_graph.html").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["averageRating"], 4.37)
        self.assertEqual(payload["ratingsCount"], 12345)

    def test_audible_search_accepts_case_spacing_and_nested_author_markup(self) -> None:
        cards = audible_catalog.parse_search_cards(
            (FIXTURES / "audible_search_variant.html").read_text(encoding="utf-8")
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Signal Fire")
        self.assertEqual(cards[0]["author"], "Jane Story")
        self.assertEqual(cards[0]["productId"], "ABC1234567")
        self.assertEqual(cards[0]["offer"]["discountPercent"], 67)


if __name__ == "__main__":
    unittest.main()
