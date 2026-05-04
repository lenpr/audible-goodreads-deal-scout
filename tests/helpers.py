from __future__ import annotations

import csv
import json
from pathlib import Path


GOODREADS_HEADERS = [
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
]


AUDIBLE_HTML = """
<html>
  <head>
    <script type="application/ld+json">
    {
      "@context":"http://schema.org",
      "@type":"Product",
      "productID":"ABC1234567",
      "name":"Signal Fire",
      "image":"https://example.com/cover.jpg",
      "offers":{"price":"14.95","priceCurrency":"USD"}
    }
    </script>
  </head>
  <body>
    <div>Get today's Daily Deal before time runs out! $4.99 Deal ends @ 11:59PM PT.</div>
    <adbl-product-metadata>
      <script type="application/json">{"authors":[{"name":"Jane Story"}]}</script>
    </adbl-product-metadata>
    <adbl-product-metadata>
      <script type="application/json">{"duration":"11 hrs and 48 mins","releaseDate":"07-12-22","categories":[{"name":"Science Fiction"},{"name":"Thriller"}]}</script>
    </adbl-product-metadata>
    <adbl-text-block slot="summary">A smart thriller with a clear Audible summary. It stays readable.</adbl-text-block>
  </body>
</html>
"""


class FakeHttpResponse:
    def __init__(self, body: str, url: str) -> None:
        self._body = body.encode("utf-8")
        self._url = url
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def fake_fetcher(_: str) -> tuple[str, str]:
    return AUDIBLE_HTML, "https://www.audible.com/pd/Signal-Fire-Audiobook/ABC1234567"


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GOODREADS_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def row(
    *,
    title: str,
    author: str,
    shelf: str = "",
    rating: str = "0",
    review: str = "",
    isbn: str = "",
    isbn13: str = "",
    bookshelves: str = "",
) -> dict[str, str]:
    return {
        "Book Id": "1",
        "Title": title,
        "Author": author,
        "Author l-f": "",
        "Additional Authors": "",
        "ISBN": isbn,
        "ISBN13": isbn13,
        "My Rating": rating,
        "Average Rating": "4.20",
        "Publisher": "",
        "Binding": "",
        "Number of Pages": "",
        "Year Published": "2022",
        "Original Publication Year": "2022",
        "Date Read": "",
        "Date Added": "2026-04-01",
        "Bookshelves": bookshelves,
        "Bookshelves with positions": "",
        "Exclusive Shelf": shelf,
        "My Review": review,
        "Spoiler": "",
        "Private Notes": "",
        "Read Count": "1",
        "Owned Copies": "0",
    }


def scan_row(book_id: str, title: str, author: str, date_added: str, *, shelf: str = "to-read") -> dict[str, str]:
    payload = row(title=title, author=author, shelf=shelf)
    payload["Book Id"] = book_id
    payload["Date Added"] = date_added
    return payload


def audible_search_card(title: str, author: str, product_id: str, offer_html: str = "") -> str:
    slug = title.replace(" ", "-")
    byline = f'<p>By: <a href="/author/{author.replace(" ", "-")}">{author}</a></p>' if author else ""
    return f"""
    <li class="productListItem">
      <h3><a href="/pd/{slug}-Audiobook/{product_id}">{title}</a></h3>
      {byline}
      <div class="buybox">{offer_html}</div>
    </li>
    """


def write_want_to_read_fixtures(
    fixture_dir: Path,
    *,
    search: dict[str, str | dict[str, str]],
    product: dict[str, str | dict[str, str]] | None = None,
) -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    manifest_search: dict[str, object] = {}
    for index, (query, html_or_failure) in enumerate(search.items()):
        if isinstance(html_or_failure, dict):
            manifest_search[query] = html_or_failure
            continue
        filename = f"search-{index}.html"
        (fixture_dir / filename).write_text(html_or_failure, encoding="utf-8")
        manifest_search[query] = filename
    manifest_product: dict[str, object] = {}
    for index, (url, html_or_failure) in enumerate((product or {}).items()):
        if isinstance(html_or_failure, dict):
            manifest_product[url] = html_or_failure
            continue
        filename = f"product-{index}.html"
        (fixture_dir / filename).write_text(html_or_failure, encoding="utf-8")
        manifest_product[url] = filename
    (fixture_dir / "manifest.json").write_text(
        json.dumps({"search": manifest_search, "product": manifest_product}, indent=2),
        encoding="utf-8",
    )


def read_message_fixture(name: str) -> str:
    return (Path(__file__).resolve().parent / "fixtures" / "messages" / name).read_text(encoding="utf-8").rstrip("\n")
