from __future__ import annotations

import csv
import gzip
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .delivery import (
    build_cron_command,
    build_cron_message,
    deliver_message,
    find_matching_cron_job,
    list_cron_jobs,
    register_cron_job,
    resolve_delivery_policy,
    resolve_delivery_settings,
    setup_configuration,
)
from .rendering import build_delivery_plan, render_delivery_summary_message, render_final_message


MIN_PYTHON = (3, 9)
HTTP_USER_AGENT = "OpenClaw Audible Goodreads Deal Scout/1.0"
DEFAULT_THRESHOLD = 3.8
DEFAULT_FRESHNESS_DAYS = 180
DEFAULT_NOTES_WARNING_CHARS = 50_000
FIT_REVIEW_SUMMARY_LIMIT = 500
SUPPORTED_PRIVACY_MODES = {"normal", "minimal"}
SUPPORTED_DELIVERY_POLICIES = {"positive_only", "always_full", "summary_on_non_match"}
DEFAULT_DELIVERY_POLICY = "positive_only"
FIT_NO_PERSONAL_DATA = "Fit: No personal preference data was configured, so this recommendation is based only on the public Goodreads score."
FIT_MODEL_UNAVAILABLE = "Fit: Personalized fit feedback is unavailable right now, but the recommendation decision still completed."
FIT_MODEL_UNAVAILABLE_TO_READ = "Fit: Strong match, on your 'to-read' shelf. Personalized fit feedback is unavailable right now, but this is already on the books you explicitly want to read."
AUTHOR_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
AUTHOR_ROLE_PATTERNS = (
    r"\bnarrated by\b",
    r"\bforeword by\b",
    r"\bafterword by\b",
    r"\bintroduction by\b",
    r"\bwith\b",
    r"\bfull cast\b",
)
AUDIBLE_BLOCK_MARKERS = (
    "captcha",
    "robot check",
    "automated access",
)
PROMOTION_MARKERS = (
    "daily deal",
    "deal ends",
    "angebot endet",
    "begrenztes angebot",
    "promotion ends",
)
PRICE_TOKEN_RE = re.compile(
    r"(?:(?P<prefix>[£$€])\s*(?P<prefix_amount>\d[\d.,]*))|(?:(?P<suffix_amount>\d[\d.,]*)\s*(?P<suffix>[£$€]))"
)
CSV_ROLE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "title": ("Title",),
    "author": ("Author",),
    "shelf": ("Exclusive Shelf",),
    "bookshelves": ("Bookshelves",),
    "rating": ("My Rating",),
    "review": ("My Review",),
    "average_rating": ("Average Rating",),
    "date_read": ("Date Read",),
    "date_added": ("Date Added",),
    "isbn": ("ISBN",),
    "isbn13": ("ISBN13",),
    "book_id": ("Book Id",),
}

UNICODE_BOLD_TRANSLATION = str.maketrans(
    {
        **{chr(ord("A") + index): chr(0x1D5D4 + index) for index in range(26)},
        **{chr(ord("a") + index): chr(0x1D5EE + index) for index in range(26)},
        **{chr(ord("0") + index): chr(0x1D7EC + index) for index in range(10)},
    }
)


def _marketplace_specs() -> dict[str, dict[str, str]]:
    return {
        "us": {
            "label": "Audible US",
            "dealUrl": "https://www.audible.com/dailydeal",
            "timezone": "America/Los_Angeles",
            "currency": "USD",
            "currencySymbol": "$",
            "defaultCron": "15 1 * * *",
        },
        "uk": {
            "label": "Audible UK",
            "dealUrl": "https://www.audible.co.uk/dailydeal",
            "timezone": "Europe/London",
            "currency": "GBP",
            "currencySymbol": "£",
            "defaultCron": "15 1 * * *",
        },
        "de": {
            "label": "Audible DE",
            "dealUrl": "https://www.audible.de/dailydeal",
            "timezone": "Europe/Berlin",
            "currency": "EUR",
            "currencySymbol": "€",
            "defaultCron": "15 1 * * *",
        },
        "ca": {
            "label": "Audible CA",
            "dealUrl": "https://www.audible.ca/dailydeal",
            "timezone": "America/Toronto",
            "currency": "CAD",
            "currencySymbol": "$",
            "defaultCron": "15 1 * * *",
        },
        "au": {
            "label": "Audible AU",
            "dealUrl": "https://www.audible.com.au/dailydeal",
            "timezone": "Australia/Sydney",
            "currency": "AUD",
            "currencySymbol": "$",
            "defaultCron": "15 1 * * *",
        },
    }


SUPPORTED_MARKETPLACES = _marketplace_specs()


class AudibleBlockedError(RuntimeError):
    pass


class AudibleFetchError(RuntimeError):
    pass


class AudibleParseError(RuntimeError):
    pass


class NoActivePromotionError(RuntimeError):
    pass


def ensure_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        raise RuntimeError(f"Python {required}+ is required for this skill.")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<br\s*/?>", ". ", text, flags=re.I)
    text = re.sub(r"</p>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_parentheticals(value: str) -> str:
    return re.sub(r"\((?:[^)]*)\)", " ", value or "")


def strip_combining_marks(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))


def normalized_key(value: str, *, ascii_only: bool = False) -> str:
    text = html.unescape(value or "").replace("&", " and ").replace("’", "'")
    text = strip_parentheticals(text)
    text = unicodedata.normalize("NFKC", text).casefold()
    if ascii_only:
        text = strip_combining_marks(text).encode("ascii", "ignore").decode("ascii")
    normalized = "".join(ch if ch.isalnum() else " " for ch in text)
    return normalize_space(normalized)


def split_author_roles(value: str) -> str:
    cleaned = normalize_space(strip_html(value))
    lowered = cleaned.casefold()
    for pattern in AUTHOR_ROLE_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            cleaned = cleaned[: match.start()].strip(" ,;-")
            lowered = cleaned.casefold()
    return normalize_space(cleaned)


def normalize_author_key(value: str, *, ascii_only: bool = False) -> str:
    cleaned = split_author_roles(value)
    normalized = normalized_key(cleaned, ascii_only=ascii_only)
    tokens = [token for token in normalized.split() if token and token not in AUTHOR_SUFFIXES]
    filtered: list[str] = []
    for index, token in enumerate(tokens):
        if len(token) == 1 and 0 < index < len(tokens) - 1:
            continue
        filtered.append(token)
    return " ".join(filtered).strip()


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_rating(value: Any) -> int:
    try:
        return int(str(value or "").strip() or 0)
    except Exception:
        return 0


def parse_int_value(value: Any) -> int | None:
    text = normalize_space(str(value or "")).replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def parse_localized_price(raw: str | None) -> float | None:
    if raw in (None, ""):
        return None
    text = normalize_space(str(raw)).replace("\xa0", "")
    match = PRICE_TOKEN_RE.search(text)
    if not match:
        plain = re.search(r"(\d[\d.,]*)", text)
        if not plain:
            return None
        number = plain.group(1)
    else:
        number = match.group("prefix_amount") or match.group("suffix_amount") or ""
    number = number.replace(" ", "")
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(".", "").replace(",", ".")
    else:
        number = number.replace(",", "")
    try:
        return float(number)
    except Exception:
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_json_atomic(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def workspace_root() -> Path:
    root = skill_root().resolve()
    for parent in [root, *root.parents]:
        if parent.name == "skills":
            return parent.parent
    return root


def default_storage_dir() -> Path:
    return workspace_root() / ".audible-goodreads-deal-scout"


def default_config_path() -> Path:
    return default_storage_dir() / "config.json"


def default_state_path() -> Path:
    return default_storage_dir() / "state.json"


def default_preferences_path() -> Path:
    return default_storage_dir() / "preferences.md"


def default_artifact_dir() -> Path:
    return default_storage_dir() / "artifacts" / "current"


def validate_marketplace(marketplace: str) -> dict[str, str]:
    key = normalize_space(marketplace).lower()
    if key not in SUPPORTED_MARKETPLACES:
        supported = ", ".join(sorted(SUPPORTED_MARKETPLACES))
        raise ValueError(f"Unsupported marketplace '{marketplace}'. Public v1 supports: {supported}.")
    return {"key": key, **SUPPORTED_MARKETPLACES[key]}


def validate_timezone(spec: dict[str, str]) -> str:
    timezone_name = spec["timezone"]
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Timezone data for {timezone_name} is unavailable on this host. Fix timezone data before enabling scheduling."
        ) from exc
    return timezone_name


def config_template(**overrides: Any) -> dict[str, Any]:
    payload = {
        "audibleMarketplace": "us",
        "threshold": DEFAULT_THRESHOLD,
        "goodreadsCsvPath": None,
        "preferencesPath": None,
        "privacyMode": "normal",
        "stateFile": None,
        "artifactDir": None,
        "freshnessDays": DEFAULT_FRESHNESS_DAYS,
        "csvColumns": {},
        "audibleDealUrl": None,
        "dailyCron": None,
        "deliveryChannel": None,
        "deliveryTarget": None,
        "deliveryPolicy": DEFAULT_DELIVERY_POLICY,
    }
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return payload


def load_config(config_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = (config_path or default_config_path()).resolve()
    payload = read_json(path, config_template())
    if not isinstance(payload, dict):
        payload = config_template()
    return path, {**config_template(), **payload}


def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    raw = input(f"{text}{suffix}: ").strip()
    if raw:
        return raw
    return default or ""


def resolve_notes_text(notes_file: str | None, inline_notes: str | None) -> str:
    if inline_notes:
        return str(inline_notes)
    normalized_path = normalize_space(str(notes_file or ""))
    if normalized_path:
        notes_path = Path(normalized_path).expanduser()
        if notes_path.exists():
            return notes_path.read_text(encoding="utf-8")
    return ""


def parse_csv_column_overrides(items: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Invalid --csv-column value '{item}'. Use role=Header.")
        role, header = item.split("=", 1)
        role = normalize_space(role).replace("-", "_").lower()
        header = header.strip()
        if not role or not header:
            raise ValueError(f"Invalid --csv-column value '{item}'. Use role=Header.")
        result[role] = header
    return result


def resolve_csv_headers(headers: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    overrides = overrides or {}
    mapping: dict[str, str] = {}
    header_lookup = {header.casefold(): header for header in headers}
    for role, names in CSV_ROLE_DEFAULTS.items():
        if role in overrides:
            mapping[role] = overrides[role]
            continue
        for candidate in names:
            existing = header_lookup.get(candidate.casefold())
            if existing:
                mapping[role] = existing
                break
    required_roles = ("title", "author", "shelf")
    missing = [role for role in required_roles if role not in mapping]
    if missing:
        detected = ", ".join(headers)
        wanted = " ".join(
            f"--csv-column {role}=..." for role in required_roles
        )
        raise ValueError(
            "Could not identify the Goodreads export columns for title, author, and shelf. "
            f"Detected headers: {detected}. Use {wanted}"
        )
    return mapping


def canonicalize_bookshelves(raw_shelf: str, raw_bookshelves: str) -> tuple[str, list[str]]:
    values = [normalize_space(raw_shelf)]
    values.extend(normalize_space(item) for item in str(raw_bookshelves or "").split(","))
    shelves: list[str] = []
    for value in values:
        if value and value not in shelves:
            shelves.append(value)
    exclusive = shelves[0] if shelves else ""
    return exclusive, shelves


def effective_shelf(entry: dict[str, Any]) -> str:
    exclusive = normalize_space(str(entry.get("exclusiveShelf") or "")).casefold()
    shelves = [normalize_space(str(item)).casefold() for item in entry.get("bookshelves") or []]
    if "read" in shelves or exclusive == "read":
        return "read"
    if "currently-reading" in shelves or exclusive == "currently-reading":
        return "currently-reading"
    if "to-read" in shelves or exclusive == "to-read":
        return "to-read"
    return ""


def load_goodreads_csv(export_path: Path, csv_columns: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with export_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        mapping = resolve_csv_headers(headers, csv_columns)
        rows: list[dict[str, Any]] = []
        for raw in reader:
            title = normalize_space(raw.get(mapping["title"]) or "")
            author = normalize_space(raw.get(mapping["author"]) or "")
            exclusive, shelves = canonicalize_bookshelves(
                raw.get(mapping["shelf"]) or "",
                raw.get(mapping.get("bookshelves", "")) or "",
            )
            row = {
                "bookId": normalize_space(raw.get(mapping.get("book_id", "")) or ""),
                "title": title,
                "author": author,
                "exclusiveShelf": exclusive,
                "bookshelves": shelves,
                "myRating": parse_rating(raw.get(mapping.get("rating", ""))),
                "myReview": normalize_space(strip_html(raw.get(mapping.get("review", "")) or "")),
                "averageRating": parse_float(raw.get(mapping.get("average_rating", ""))),
                "dateRead": normalize_space(raw.get(mapping.get("date_read", "")) or ""),
                "dateAdded": normalize_space(raw.get(mapping.get("date_added", "")) or ""),
                "isbn": normalize_space(raw.get(mapping.get("isbn", "")) or ""),
                "isbn13": normalize_space(raw.get(mapping.get("isbn13", "")) or ""),
            }
            rows.append(row)
    stats = {
        "headers": headers,
        "columnMap": mapping,
        "totalRows": len(rows),
        "ratedOrReviewedRows": sum(1 for row in rows if row["myRating"] > 0 or row["myReview"]),
        "missingAuthorRows": sum(1 for row in rows if row["title"] and not row["author"]),
    }
    return rows, stats


def strong_personal_matches(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    candidate_isbns = {normalize_space(str(candidate.get("isbn") or "")), normalize_space(str(candidate.get("isbn13") or ""))}
    candidate_isbns.discard("")
    normalized_title = normalized_key(candidate.get("title") or "", ascii_only=True)
    normalized_author = normalize_author_key(candidate.get("author") or "", ascii_only=True)
    for row in rows:
        row_title = normalized_key(str(row.get("title") or ""), ascii_only=True)
        row_author = normalize_author_key(str(row.get("author") or ""), ascii_only=True)
        row_isbns = {normalize_space(str(row.get("isbn") or "")), normalize_space(str(row.get("isbn13") or ""))}
        row_isbns.discard("")
        if candidate_isbns and row_isbns and candidate_isbns & row_isbns:
            matches.append(row)
            continue
        if row_title and row_author and row_title == normalized_title and row_author == normalized_author:
            matches.append(row)
    return matches


def classify_personal_match(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    matches = strong_personal_matches(candidate, rows)
    if not matches:
        return {"matched": False, "effectiveShelf": "", "matches": []}
    effective_states = {state for state in (effective_shelf(item) for item in matches) if state in {"read", "currently-reading", "to-read"}}
    if len(effective_states) > 1:
        return {"matched": True, "ambiguous": True, "effectiveShelf": "", "matches": matches}
    state = next(iter(effective_states), "")
    return {"matched": True, "ambiguous": False, "effectiveShelf": state, "matches": matches}


def truncate_text(text: str, limit: int) -> str:
    text = normalize_space(text)
    if len(text) <= limit:
        return text
    clipped = text[: max(0, limit - 1)].rstrip(" ,;:")
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:") + "…"


def decode_response_bytes(raw: bytes, content_encoding: str) -> str:
    encoding = normalize_space(content_encoding).lower()
    if encoding == "gzip":
        return gzip.decompress(raw).decode("utf-8", "ignore")
    if encoding == "deflate":
        try:
            return zlib.decompress(raw).decode("utf-8", "ignore")
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", "ignore")
    return raw.decode("utf-8", "ignore")


def fetch_text_with_final_url(url: str, *, retries: int = 2, backoff_seconds: float = 1.0) -> tuple[str, str]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": HTTP_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                text = decode_response_bytes(raw, str(response.headers.get("Content-Encoding") or ""))
                lowered = text.lower()
                if any(marker in lowered for marker in AUDIBLE_BLOCK_MARKERS):
                    raise AudibleBlockedError(f"Audible blocked the request for {url}.")
                return text, str(response.geturl() or url)
        except HTTPError as exc:
            last_error = exc
            if exc.code in {403, 429}:
                raise AudibleBlockedError(f"Audible request blocked with HTTP {exc.code}.") from exc
            if attempt >= retries:
                break
        except URLError as exc:
            last_error = exc
            if attempt >= retries:
                break
        except AudibleBlockedError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
        time.sleep(backoff_seconds * (attempt + 1))
    raise AudibleFetchError(f"Audible request failed for {url}: {last_error}")


def parse_json_ld_blocks(html_text: str) -> list[Any]:
    payloads: list[Any] = []
    for match in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.I | re.S):
        raw = html.unescape((match.group(1) or "").strip())
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except Exception:
            continue
    return payloads


def flatten_json_ld_items(html_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in parse_json_ld_blocks(html_text):
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict):
                items.append(item)
    return items


def find_json_ld_product(html_text: str) -> dict[str, Any] | None:
    for item in flatten_json_ld_items(html_text):
        if item.get("@type") == "Product":
            return item
    return None


def extract_audible_metadata_blocks(html_text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    matches = re.findall(
        r'<adbl-product-metadata[^>]*>.*?<script type="application/json">(.*?)</script>',
        html_text,
        re.I | re.S,
    )
    for raw_match in matches:
        try:
            payload = json.loads(html.unescape((raw_match or "").strip()))
        except Exception:
            continue
        if isinstance(payload, dict):
            blocks.append(payload)
    return blocks


def parse_author_entities(raw: Any) -> list[str]:
    values: list[str] = []
    if isinstance(raw, str):
        cleaned = normalize_space(raw)
        if cleaned:
            values.append(cleaned)
        return values
    if isinstance(raw, dict):
        name = normalize_space(str(raw.get("name") or ""))
        if name:
            values.append(name)
        return values
    if isinstance(raw, list):
        for item in raw:
            values.extend(parse_author_entities(item))
    return values


def merge_audible_metadata(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"authors": [], "categories": []}
    for block in blocks:
        if not merged.get("duration") and block.get("duration"):
            merged["duration"] = block.get("duration")
        if not merged.get("releaseDate") and block.get("releaseDate"):
            merged["releaseDate"] = block.get("releaseDate")
        if not merged.get("authors") and block.get("authors"):
            merged["authors"] = parse_author_entities(block.get("authors"))
        if isinstance(block.get("categories"), list):
            categories = merged.setdefault("categories", [])
            for item in block["categories"]:
                if not isinstance(item, dict):
                    continue
                label = normalize_space(str(item.get("name") or ""))
                if label and label not in categories:
                    categories.append(label)
    return merged


def extract_json_ld_authors(html_text: str) -> list[str]:
    authors: list[str] = []
    for item in flatten_json_ld_items(html_text):
        for author in parse_author_entities(item.get("author")):
            if author and author not in authors:
                authors.append(author)
    return authors


def parse_audible_summary(html_text: str) -> str:
    match = re.search(r'<adbl-text-block[^>]+slot="summary"[^>]*>(.*?)</adbl-text-block>', html_text, re.I | re.S)
    if match:
        return strip_html(match.group(1))
    return ""


def is_plausible_genre_label(value: str) -> bool:
    label = normalize_space(strip_html(value))
    if not label:
        return False
    if len(label) > 48:
        return False
    if len(label.split()) > 5:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]", label):
        return False
    if re.search(r"[{}\"]", label):
        return False
    if re.search(r"(?:https?://|www\.)", label, re.I):
        return False
    if re.search(r"(?:\b\d{2,}\b|[$£€])", label):
        return False
    lowered = label.casefold()
    blocked_substrings = (
        "sign in",
        "audible",
        "daily deal",
        "subscribe",
        "podcast",
        "customer",
        "copy link",
        "buy for",
        "try for",
        "add to",
        "help center",
        "please try again",
        "preview",
        "cancel anytime",
        "no results",
        "suggested searches",
        "narrated by",
        "english español",
    )
    return not any(marker in lowered for marker in blocked_substrings)


def parse_audible_chip_genres(html_text: str) -> list[str]:
    genres: list[str] = []
    for match in re.finditer(r'<adbl-chip\b[^>]*>(.*?)</adbl-chip>', html_text, re.I | re.S):
        label = normalize_space(strip_html(match.group(1)))
        if is_plausible_genre_label(label) and label not in genres:
            genres.append(label)
    return genres


def parse_audible_author_fallback(html_text: str) -> str:
    matches = re.findall(r'<a href="/author/[^"]+">([^<]+)</a>', html_text, re.I)
    if matches:
        return normalize_space(strip_html(matches[0]))
    match = re.search(r"By:\s*</span>\s*<a[^>]*>([^<]+)</a>", html_text, re.I | re.S)
    if match:
        return normalize_space(strip_html(match.group(1)))
    return ""


def parse_audible_author_info(html_text: str, metadata: dict[str, Any]) -> tuple[str, str, str]:
    metadata_authors = parse_author_entities(metadata.get("authors"))
    if metadata_authors:
        author = split_author_roles(metadata_authors[0])
        if author:
            return author, "structured_metadata", "high"
    json_ld_authors = extract_json_ld_authors(html_text)
    if json_ld_authors:
        author = split_author_roles(json_ld_authors[0])
        if author:
            return author, "json_ld", "high"
    fallback_author = split_author_roles(parse_audible_author_fallback(html_text))
    if fallback_author:
        return fallback_author, "html_fallback", "low"
    return "", "missing", "missing"


def parse_release_year(raw_release_date: str | None) -> int | None:
    text = normalize_space(raw_release_date or "")
    if not text:
        return None
    for fmt in ("%m-%d-%y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).year
        except Exception:
            continue
    match = re.search(r"(\d{4})", text)
    if match:
        return int(match.group(1))
    return None


def parse_audible_member_hidden(html_text: str) -> bool:
    lowered = html_text.lower()
    return "member only" in lowered or "logged in to redeem this offer price" in lowered


def first_price_near_markers(html_text: str) -> float | None:
    original = normalize_space(strip_html(html_text))
    lowered = original.casefold()
    for marker in PROMOTION_MARKERS:
        offset = lowered.find(marker)
        if offset < 0:
            continue
        window = original[max(0, offset - 80) : offset + 260]
        match = PRICE_TOKEN_RE.search(window)
        if match:
            return parse_localized_price(match.group(0))
    return None


def parse_list_price(product_json: dict[str, Any], html_text: str) -> float | None:
    offers = product_json.get("offers") if isinstance(product_json.get("offers"), dict) else {}
    if isinstance(offers, dict):
        price = parse_localized_price(str(offers.get("price") or ""))
        if price is not None:
            return price
    buy_now = re.search(r"(?:buy now for|für|acheter pour|buy for)\s*([^<\n]+)", html_text, re.I)
    if buy_now:
        price = parse_localized_price(buy_now.group(1))
        if price is not None:
            return price
    return None


def parse_isbn_like(product_json: dict[str, Any], metadata: dict[str, Any], html_text: str, field_name: str) -> str:
    for source in (product_json, metadata):
        value = normalize_space(str(source.get(field_name) or ""))
        if value:
            return value
    match = re.search(rf"{field_name}\D+([0-9Xx-]+)", html_text, re.I)
    if match:
        return normalize_space(match.group(1))
    return ""


def parse_audible_deal(html_text: str, final_url: str, requested_url: str) -> dict[str, Any]:
    product_json = find_json_ld_product(html_text) or {}
    metadata = merge_audible_metadata(extract_audible_metadata_blocks(html_text))
    title = normalize_space(str(product_json.get("name") or ""))
    if not title:
        title_match = re.search(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", html_text, re.I | re.S)
        title = normalize_space(strip_html(title_match.group(1) if title_match else ""))
    if not title:
        raise AudibleParseError("failed to parse Audible title")

    author, author_source, author_reliability = parse_audible_author_info(html_text, metadata)
    if not author:
        raise AudibleParseError("failed to parse Audible author")

    summary = parse_audible_summary(html_text)
    if not summary:
        raise AudibleParseError("failed to parse Audible summary")

    sale_price = first_price_near_markers(html_text)
    member_hidden = parse_audible_member_hidden(html_text)
    if sale_price is None and not member_hidden:
        raise NoActivePromotionError(f"No active daily promotion was found at {requested_url}.")

    runtime = normalize_space(str(metadata.get("duration") or "")) or normalize_space(str(product_json.get("duration") or "")) or "Unknown"
    year = parse_release_year(str(metadata.get("releaseDate") or "")) or parse_release_year(str(product_json.get("datePublished") or ""))
    cover_url = normalize_space(str(product_json.get("image") or ""))
    if not cover_url:
        meta_image = re.search(r'<meta property="og:image" content="([^"]+)"', html_text, re.I)
        cover_url = html.unescape(meta_image.group(1)) if meta_image else ""

    genres: list[str] = []
    for label in list(metadata.get("categories") or []) + parse_audible_chip_genres(html_text):
        cleaned = normalize_space(str(label))
        if cleaned and cleaned not in genres:
            genres.append(cleaned)

    product_id = normalize_space(str(product_json.get("productID") or ""))
    if not product_id:
        match = re.search(r"/pd/(?:[^/]+/)?([A-Z0-9]{10})", final_url)
        product_id = match.group(1) if match else ""

    return {
        "productId": product_id or normalized_key(title, ascii_only=True),
        "title": title,
        "author": author,
        "authorSource": author_source,
        "authorReliability": author_reliability,
        "summary": truncate_text(summary, 1200),
        "runtime": runtime,
        "year": year,
        "genres": genres[:4],
        "coverUrl": cover_url,
        "salePrice": sale_price,
        "listPrice": parse_list_price(product_json, html_text),
        "memberHidden": member_hidden,
        "audibleUrl": final_url,
        "isbn": parse_isbn_like(product_json, metadata, html_text, "isbn"),
        "isbn13": parse_isbn_like(product_json, metadata, html_text, "isbn13"),
    }


def export_age_days(export_path: Path, logical_run_date: date) -> int:
    modified = datetime.fromtimestamp(export_path.stat().st_mtime, tz=UTC).date()
    return max(0, (logical_run_date - modified).days)


def logical_store_date(spec: dict[str, str], raw_today: str | None = None) -> date:
    if raw_today:
        return date.fromisoformat(raw_today)
    now_utc = datetime.now(UTC)
    return now_utc.astimezone(ZoneInfo(spec["timezone"])).date()


def build_deal_key(spec: dict[str, str], candidate: dict[str, Any], store_date: date) -> str:
    product_id = normalize_space(str(candidate.get("productId") or ""))
    if not product_id:
        parsed = urllib.parse.urlparse(str(candidate.get("audibleUrl") or ""))
        product_id = normalize_space(Path(parsed.path).stem or parsed.path.rstrip("/").rsplit("/", 1)[-1])
    return f"{spec['key']}:{store_date.isoformat()}:{product_id}"


def default_state() -> dict[str, Any]:
    return {
        "lastEmittedDealKey": None,
        "lastStaleWarningDate": None,
        "updatedAt": None,
    }


def load_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return default_state()
    payload = read_json(path, default_state())
    if not isinstance(payload, dict):
        return default_state()
    merged = {**default_state(), **payload}
    return merged


def save_state(path: Path, state: dict[str, Any]) -> None:
    payload = {**default_state(), **state, "updatedAt": now_iso()}
    write_json_atomic(path, payload)


def approx_token_count(text: str) -> int:
    return max(0, round(len(text) / 4))


def normalize_review_text(review: str) -> str:
    cleaned = normalize_space(review)
    cleaned = re.sub(r"([.!?])(?:\s*[.!?]){1,}", r"\1", cleaned)
    return cleaned


def build_fit_context_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        entry = {
            "entryId": index,
            "title": normalize_space(str(row.get("title") or "")),
            "author": normalize_space(str(row.get("author") or "")),
            "rating": int(row.get("myRating") or 0),
            "shelf": effective_shelf(row) or normalize_space(str(row.get("exclusiveShelf") or "")),
        }
        if normalize_review_text(str(row.get("myReview") or "")):
            entry["hasReview"] = True
        entries.append(entry)
    return entries


def build_review_source_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        review = normalize_review_text(str(row.get("myReview") or ""))
        if not review:
            continue
        entries.append(
            {
                "entryId": index,
                "title": normalize_space(str(row.get("title") or "")),
                "author": normalize_space(str(row.get("author") or "")),
                "rating": int(row.get("myRating") or 0),
                "shelf": effective_shelf(row) or normalize_space(str(row.get("exclusiveShelf") or "")),
                "reviewText": review,
            }
        )
    return entries


def build_fit_context(rated_or_reviewed_entries: list[dict[str, Any]]) -> dict[str, Any]:
    review_entries = build_review_source_entries(rated_or_reviewed_entries)
    review_count = sum(1 for row in rated_or_reviewed_entries if normalize_space(str(row.get("myReview") or "")))
    rating_distribution: dict[str, int] = {}
    for rating in range(1, 6):
        count = sum(1 for row in rated_or_reviewed_entries if int(row.get("myRating") or 0) == rating)
        if count:
            rating_distribution[str(rating)] = count
    return {
        "schemaVersion": 1,
        "entryCount": len(rated_or_reviewed_entries),
        "reviewCount": review_count,
        "ratingDistribution": rating_distribution,
        "entries": build_fit_context_entries(rated_or_reviewed_entries),
        "reviewSourceCount": len(review_entries),
    }


def build_review_source(rated_or_reviewed_entries: list[dict[str, Any]]) -> dict[str, Any]:
    entries = build_review_source_entries(rated_or_reviewed_entries)
    return {
        "schemaVersion": 1,
        "summaryLimitChars": FIT_REVIEW_SUMMARY_LIMIT,
        "entryCount": len(entries),
        "entries": entries,
    }


def build_context_budget(
    rated_or_reviewed_entries: list[dict[str, Any]],
    fit_context: dict[str, Any],
    review_source: dict[str, Any] | None,
    notes_text: str,
) -> dict[str, Any]:
    legacy_json = json.dumps(rated_or_reviewed_entries, sort_keys=True, ensure_ascii=False)
    fit_context_json = json.dumps(fit_context, sort_keys=True, ensure_ascii=False)
    review_source_json = json.dumps(review_source or {}, sort_keys=True, ensure_ascii=False)
    legacy_chars = len(legacy_json)
    fit_context_chars = len(fit_context_json)
    review_source_chars = len(review_source_json)
    review_count = int((review_source or {}).get("entryCount") or 0)
    estimated_review_summary_chars = review_count * FIT_REVIEW_SUMMARY_LIMIT
    estimated_final_chars = fit_context_chars + estimated_review_summary_chars
    notes_chars = len(notes_text)
    savings_chars = max(0, legacy_chars - estimated_final_chars)
    savings_percent = 0.0
    if legacy_chars:
        savings_percent = round((savings_chars / legacy_chars) * 100, 1)
    return {
        "legacyChars": legacy_chars,
        "legacyApproxTokens": approx_token_count(legacy_json),
        "fitContextBaseChars": fit_context_chars,
        "fitContextBaseApproxTokens": approx_token_count(fit_context_json),
        "reviewSourceRawChars": review_source_chars,
        "reviewSourceRawApproxTokens": approx_token_count(review_source_json),
        "estimatedReviewSummaryChars": estimated_review_summary_chars,
        "estimatedReviewSummaryApproxTokens": max(0, round(estimated_review_summary_chars / 4)),
        "estimatedFinalChars": estimated_final_chars,
        "estimatedFinalApproxTokens": max(0, round(estimated_final_chars / 4)),
        "savingsChars": savings_chars,
        "savingsPercent": savings_percent,
        "notesChars": notes_chars,
        "notesApproxTokens": approx_token_count(notes_text),
    }


def write_artifacts(
    artifact_dir: Path,
    audible: dict[str, Any],
    personal_data: dict[str, Any],
    fit_context: dict[str, Any] | None,
    review_source: dict[str, Any] | None,
    notes_text: str,
) -> dict[str, str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    audible_path = artifact_dir / "audible.json"
    personal_path = artifact_dir / "personal-data.json"
    write_json_atomic(audible_path, audible)
    write_json_atomic(personal_path, personal_data)
    artifacts = {
        "audiblePath": str(audible_path),
        "personalDataPath": str(personal_path),
    }
    if fit_context is not None:
        fit_context_path = artifact_dir / "fit-context.json"
        write_json_atomic(fit_context_path, fit_context)
        artifacts["fitContextPath"] = str(fit_context_path)
    if review_source is not None and int(review_source.get("entryCount") or 0) > 0:
        review_source_path = artifact_dir / "review-source.json"
        write_json_atomic(review_source_path, review_source)
        artifacts["reviewSourcePath"] = str(review_source_path)
    if notes_text:
        notes_path = artifact_dir / "preferences.md"
        atomic_write_text(notes_path, notes_text.rstrip() + "\n")
        artifacts["notesPath"] = str(notes_path)
    return artifacts


def runtime_output_schema() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "type": "object",
        "required": ["schemaVersion", "goodreads", "fit"],
        "properties": {
            "schemaVersion": {"const": 1},
            "goodreads": {
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {
                        "enum": ["resolved", "no_match", "lookup_failed"],
                    },
                    "url": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "author": {"type": ["string", "null"]},
                    "averageRating": {"type": ["number", "null"]},
                    "ratingsCount": {"type": ["integer", "null"]},
                    "evidence": {"type": ["string", "null"]},
                },
            },
            "fit": {
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {
                        "enum": ["written", "not_applicable", "unavailable"],
                    },
                    "sentence": {"type": ["string", "null"]},
                },
            },
        },
    }


def build_runtime_input(prep_result: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(prep_result.get("metadata") or {})
    personal_data = dict(prep_result.get("personalData") or {})
    exact_shelf = normalize_space(str(personal_data.get("exactShelfMatch") or ""))
    csv_data = dict(personal_data.get("csv") or {})
    context_budget = dict(csv_data.get("contextBudget") or {})
    return {
        "schemaVersion": 1,
        "decisionContract": {
            "threshold": metadata.get("threshold", DEFAULT_THRESHOLD),
            "exactShelfMatch": exact_shelf,
            "toReadOverridesThreshold": True,
            "readAndCurrentlyReadingSuppress": True,
        },
        "audible": prep_result.get("audible") or {},
        "personalDataSummary": {
            "mode": personal_data.get("mode"),
            "privacyMode": personal_data.get("privacyMode"),
            "allowModelPersonalization": personal_data.get("allowModelPersonalization"),
            "exactShelfMatch": exact_shelf,
            "matchedEntryCount": len(personal_data.get("matchedEntries") or []),
            "csvRatedOrReviewedCount": int(csv_data.get("ratedOrReviewedCount") or 0),
            "csvReviewedCount": int(csv_data.get("reviewedCount") or 0),
            "fitContextApproxTokens": int(context_budget.get("estimatedFinalApproxTokens") or 0),
            "notesPresent": bool(((personal_data.get("notes") or {}).get("present"))),
        },
        "artifactPaths": prep_result.get("artifacts") or {},
        "warnings": list(prep_result.get("warnings") or []),
        "requiredRuntimeOutputSchema": runtime_output_schema(),
    }


def build_runtime_prompt(runtime_input: dict[str, Any]) -> str:
    threshold = runtime_input["decisionContract"]["threshold"]
    exact_shelf = runtime_input["decisionContract"].get("exactShelfMatch") or ""
    lines = [
        "You are the skill runtime for audible-goodreads-deal-scout.",
        "Read the runtime input JSON and return JSON only.",
        "Do not invent fields outside the required runtime output schema.",
        "Use OpenClaw web/search to locate the Goodreads public book page and score when needed.",
        "Prefer Goodreads book pages over list, author, or discussion pages.",
        "Verify the Goodreads title/author match against the Audible title and author before trusting the score.",
        f"The public Goodreads threshold is {threshold:.1f}.",
    ]
    if exact_shelf == "to-read":
        lines.append("This book is already on the user's Goodreads to-read shelf. Goodreads lookup is optional for decisioning; a fit sentence is still useful.")
    else:
        lines.append("If Goodreads cannot be confidently matched, return goodreads.status = \"no_match\" or \"lookup_failed\" instead of guessing.")
    lines.extend(
        [
            "Fit generation rules:",
            "- If privacyMode is minimal, do not use personal CSV or notes content.",
            "- Use artifacts.fitContextPath as the primary CSV taste artifact. It keeps every rated/reviewed book and strips low-value metadata.",
            "- If artifacts.reviewSourcePath exists, summarize each review-bearing entry to 500 characters or fewer before using it for fit reasoning. Do not mechanically truncate reviews.",
            "- Use artifacts.personalDataPath for summary metadata and exact shelf state, not for full taste history.",
            "- Write Fit as a compact paragraph, not a generic single sentence.",
            "- Preferred shape: 2 or 3 short sentences, roughly 45-90 words total.",
            "- Mention what is likely to appeal to the user and one concrete thing they may dislike or find limiting.",
            "- Avoid low-entropy filler like 'your Goodreads history shows interest' unless followed by specific taste detail.",
            "- If exactShelfMatch is to-read, mention that explicitly in the fit paragraph.",
            "- If there is no meaningful personal data, set fit.status to \"not_applicable\".",
            "- If the model cannot write a fit paragraph reliably, set fit.status to \"unavailable\".",
            "",
            "Required runtime output schema:",
            json.dumps(runtime_output_schema(), indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "Runtime input JSON:",
            json.dumps(runtime_input, indent=2, sort_keys=True, ensure_ascii=False),
        ]
    )
    return "\n".join(lines) + "\n"


def write_runtime_contract_artifacts(artifact_dir: Path, prep_result: dict[str, Any]) -> dict[str, str]:
    runtime_input = build_runtime_input(prep_result)
    runtime_input_path = artifact_dir / "runtime-input.json"
    runtime_prompt_path = artifact_dir / "runtime-prompt.md"
    runtime_schema_path = artifact_dir / "runtime-output-schema.json"
    write_json_atomic(runtime_input_path, runtime_input)
    atomic_write_text(runtime_prompt_path, build_runtime_prompt(runtime_input))
    write_json_atomic(runtime_schema_path, runtime_output_schema())
    return {
        "runtimeInputPath": str(runtime_input_path),
        "runtimePromptPath": str(runtime_prompt_path),
        "runtimeOutputSchemaPath": str(runtime_schema_path),
    }


def measure_context(
    csv_path: Path,
    *,
    csv_columns: dict[str, str] | None = None,
    notes_text: str = "",
    output_path: Path | None = None,
) -> dict[str, Any]:
    rows, stats = load_goodreads_csv(csv_path, csv_columns)
    rated_or_reviewed_entries = [
        row
        for row in rows
        if row.get("myRating", 0) > 0 or normalize_space(str(row.get("myReview") or ""))
    ]
    fit_context = build_fit_context(rated_or_reviewed_entries)
    review_source = build_review_source(rated_or_reviewed_entries)
    budget = build_context_budget(rated_or_reviewed_entries, fit_context, review_source, notes_text)
    if output_path is not None:
        write_json_atomic(output_path.expanduser(), fit_context)
        if int(review_source.get("entryCount") or 0) > 0:
            review_output = output_path.expanduser().with_name(output_path.expanduser().stem + ".review-source.json")
            write_json_atomic(review_output, review_source)
    return {
        "csvPath": str(csv_path),
        "totalRows": stats.get("totalRows", 0),
        "ratedOrReviewedRows": stats.get("ratedOrReviewedRows", 0),
        "reviewedRows": fit_context.get("reviewCount", 0),
        "fitContextPath": str(output_path.expanduser()) if output_path is not None else None,
        "reviewSourcePath": str(output_path.expanduser().with_name(output_path.expanduser().stem + ".review-source.json")) if output_path is not None and int(review_source.get("entryCount") or 0) > 0 else None,
        "fitContextEntryCount": fit_context.get("entryCount", 0),
        "contextBudget": budget,
    }


def normalize_fit_sentence(sentence: str) -> str:
    cleaned = normalize_space(sentence)
    if not cleaned:
        return ""
    if not cleaned.lower().startswith("fit:"):
        cleaned = f"Fit: {cleaned}"
    return cleaned


def validate_runtime_output(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Runtime output must be a JSON object.")
    if payload.get("schemaVersion") != 1:
        raise ValueError("Runtime output schemaVersion must be 1.")
    goodreads = payload.get("goodreads")
    fit = payload.get("fit")
    if not isinstance(goodreads, dict):
        raise ValueError("Runtime output must include a goodreads object.")
    if not isinstance(fit, dict):
        raise ValueError("Runtime output must include a fit object.")
    goodreads_status = normalize_space(str(goodreads.get("status") or "")).lower()
    fit_status = normalize_space(str(fit.get("status") or "")).lower()
    if goodreads_status not in {"resolved", "no_match", "lookup_failed"}:
        raise ValueError("goodreads.status must be resolved, no_match, or lookup_failed.")
    if fit_status not in {"written", "not_applicable", "unavailable"}:
        raise ValueError("fit.status must be written, not_applicable, or unavailable.")
    return {
        "schemaVersion": 1,
        "goodreads": {
            "status": goodreads_status,
            "url": normalize_space(str(goodreads.get("url") or "")) or None,
            "title": normalize_space(str(goodreads.get("title") or "")) or None,
            "author": normalize_space(str(goodreads.get("author") or "")) or None,
            "averageRating": parse_float(goodreads.get("averageRating")),
            "ratingsCount": parse_int_value(goodreads.get("ratingsCount")),
            "evidence": normalize_space(str(goodreads.get("evidence") or "")) or None,
        },
        "fit": {
            "status": fit_status,
            "sentence": normalize_fit_sentence(str(fit.get("sentence") or "")) or None,
        },
    }


def price_display(audible: dict[str, Any], marketplace_key: str) -> str:
    spec = SUPPORTED_MARKETPLACES.get(marketplace_key, SUPPORTED_MARKETPLACES["us"])
    symbol = spec["currencySymbol"]
    sale = audible.get("salePrice")
    list_price = audible.get("listPrice")
    if sale is not None and list_price is not None and list_price > 0:
        discount = max(0, round((1 - (sale / list_price)) * 100))
        return f"Price: {symbol}{sale:.2f} (-{discount}%, list price {symbol}{list_price:.2f})"
    if sale is not None:
        return f"Price: {symbol}{sale:.2f}"
    if audible.get("memberHidden"):
        return "Price: member deal / hidden"
    return "Price: unavailable"


def offer_description(audible: dict[str, Any]) -> str:
    summary = normalize_space(str(audible.get("summary") or ""))
    if not summary:
        return ""
    return truncate_text(summary, 520)


def format_runtime(runtime: str) -> str:
    text = normalize_space(runtime)
    match = re.fullmatch(r"(\d+)\s*hrs?\s*and\s*(\d+)\s*mins?", text, re.I)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        return f"{hours}:{minutes:02d} hrs"
    return text


def bold_visible_text(value: str) -> str:
    return str(value or "").translate(UNICODE_BOLD_TRANSLATION)


def normalize_delivery_policy(value: str | None) -> str:
    policy = normalize_space(str(value or "")).lower() or DEFAULT_DELIVERY_POLICY
    if policy not in SUPPORTED_DELIVERY_POLICIES:
        return DEFAULT_DELIVERY_POLICY
    return policy


def finalize_skill_result(prep_result: dict[str, Any], runtime_output: dict[str, Any] | None = None) -> dict[str, Any]:
    if prep_result.get("status") in {"suppress", "error"}:
        final_result = {
            "schemaVersion": 1,
            "status": prep_result["status"],
            "reasonCode": prep_result["reasonCode"],
            "reasonText": prep_result.get("message"),
            "warnings": list(prep_result.get("warnings") or []),
            "audible": prep_result.get("audible") or {},
            "goodreads": {"status": "not_needed"},
            "fitSentence": "",
            "metadata": prep_result.get("metadata") or {},
        }
        final_result["message"] = render_final_message(final_result)
        return final_result

    validated_runtime = validate_runtime_output(runtime_output or {"schemaVersion": 1, "goodreads": {"status": "lookup_failed"}, "fit": {"status": "unavailable"}})
    personal_data = dict(prep_result.get("personalData") or {})
    exact_shelf = normalize_space(str(personal_data.get("exactShelfMatch") or ""))
    warnings = list(prep_result.get("warnings") or [])

    if validated_runtime["fit"]["status"] == "written" and validated_runtime["fit"]["sentence"]:
        fit_sentence = validated_runtime["fit"]["sentence"]
    elif exact_shelf == "to-read" and personal_data.get("allowModelPersonalization"):
        fit_sentence = FIT_MODEL_UNAVAILABLE_TO_READ
    elif personal_data.get("allowModelPersonalization"):
        fit_sentence = FIT_MODEL_UNAVAILABLE
    else:
        fit_sentence = FIT_NO_PERSONAL_DATA

    if exact_shelf == "to-read":
        reason_code = "recommend_to_read_override"
        reason_text = "Saved on your Goodreads to-read shelf."
        status = "recommend"
    else:
        goodreads = validated_runtime["goodreads"]
        if goodreads["status"] == "lookup_failed":
            reason_code = "error_goodreads_lookup_failed"
            reason_text = "Goodreads public lookup failed."
            status = "error"
        elif goodreads["status"] == "no_match":
            reason_code = "suppress_no_goodreads_match"
            reason_text = "No matching Goodreads book page could be confirmed."
            status = "suppress"
        else:
            threshold = float((prep_result.get("metadata") or {}).get("threshold") or DEFAULT_THRESHOLD)
            rating = goodreads.get("averageRating")
            if rating is None:
                reason_code = "error_goodreads_lookup_failed"
                reason_text = "Goodreads lookup did not return a usable public score."
                status = "error"
            elif rating <= threshold:
                reason_code = "suppress_below_goodreads_threshold"
                reason_text = f"Goodreads public score {rating:.2f} did not clear the {threshold:.1f} threshold."
                status = "suppress"
            else:
                reason_code = "recommend_public_threshold"
                reason_text = f"Goodreads public score {rating:.2f} cleared the {threshold:.1f} threshold."
                status = "recommend"

    final_result = {
        "schemaVersion": 1,
        "status": status,
        "reasonCode": reason_code,
        "reasonText": reason_text,
        "warnings": warnings,
        "audible": prep_result.get("audible") or {},
        "goodreads": validated_runtime["goodreads"],
        "fitSentence": fit_sentence,
        "metadata": prep_result.get("metadata") or {},
    }
    final_result["message"] = render_final_message(final_result)
    return final_result


def effective_mode(csv_path: Path | None, notes_text: str) -> tuple[str, str]:
    if csv_path and notes_text:
        return "full", "ready_full"
    if csv_path:
        return "full", "ready_full"
    if notes_text:
        return "notes", "ready_notes"
    return "public", "ready_public"


def prepare_run(
    options: dict[str, Any],
    *,
    fetcher: Callable[[str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    ensure_python_version()
    fetcher = fetcher or (lambda url: fetch_text_with_final_url(url))
    config_path = Path(options["configPath"]).resolve() if options.get("configPath") else None
    _, file_config = load_config(config_path)
    merged = {**file_config, **{key: value for key, value in options.items() if value is not None}}

    marketplace = str(merged.get("audibleMarketplace") or "us").lower()
    try:
        spec = validate_marketplace(marketplace)
    except ValueError as exc:
        return {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_unsupported_marketplace",
            "warnings": [],
            "audible": {},
            "personalData": {},
            "artifacts": {},
            "metadata": {"supportedMarketplaces": sorted(SUPPORTED_MARKETPLACES)},
            "message": str(exc),
        }

    warnings: list[str] = []
    invocation_mode = normalize_space(str(merged.get("invocationMode") or "manual")).lower() or "manual"
    threshold = float(merged.get("threshold") or DEFAULT_THRESHOLD)
    privacy_mode = normalize_space(str(merged.get("privacyMode") or "normal")).lower() or "normal"
    if privacy_mode not in SUPPORTED_PRIVACY_MODES:
        privacy_mode = "normal"

    notes_file = normalize_space(str(merged.get("preferencesPath") or merged.get("notesFile") or ""))
    notes_text = resolve_notes_text(notes_file, str(merged.get("notesText") or ""))

    notes_warning_chars = int(merged.get("notesWarningChars") or DEFAULT_NOTES_WARNING_CHARS)
    if notes_text and len(notes_text) > notes_warning_chars:
        warnings.append(
            f"Preference notes are {len(notes_text)} characters; fit generation may be slower."
        )

    csv_columns = dict(merged.get("csvColumns") or {})
    if merged.get("csvColumnOverrides"):
        csv_columns.update(dict(merged["csvColumnOverrides"]))

    csv_path = None
    if merged.get("goodreadsCsvPath"):
        csv_path = Path(str(merged["goodreadsCsvPath"])).expanduser()
        if not csv_path.exists():
            return {
                "schemaVersion": 1,
                "status": "error",
                "reasonCode": "error_missing_csv",
                "warnings": warnings,
                "audible": {},
                "personalData": {},
                "artifacts": {},
                "metadata": {"marketplace": spec["key"]},
                "message": f"Goodreads CSV not found at {csv_path}.",
            }

    mode, ready_reason = effective_mode(csv_path, notes_text)
    requested_url = normalize_space(str(merged.get("audibleDealUrl") or spec["dealUrl"]))
    store_date = logical_store_date(spec, merged.get("today"))
    try:
        html_text, final_url = fetcher(requested_url)
        candidate = parse_audible_deal(html_text, final_url, requested_url)
    except NoActivePromotionError as exc:
        return {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_no_active_promotion",
            "warnings": warnings,
            "audible": {"marketplace": spec["key"], "requestedUrl": requested_url},
            "personalData": {"mode": mode, "privacyMode": privacy_mode},
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "shortCircuit": True,
            },
            "message": str(exc),
        }
    except AudibleBlockedError as exc:
        return {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_audible_blocked",
            "warnings": warnings,
            "audible": {"marketplace": spec["key"], "requestedUrl": requested_url},
            "personalData": {"mode": mode, "privacyMode": privacy_mode},
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "shortCircuit": True,
            },
            "message": str(exc),
        }
    except AudibleFetchError as exc:
        return {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_audible_fetch_failed",
            "warnings": warnings,
            "audible": {"marketplace": spec["key"], "requestedUrl": requested_url},
            "personalData": {"mode": mode, "privacyMode": privacy_mode},
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "shortCircuit": True,
            },
            "message": str(exc),
        }
    except AudibleParseError as exc:
        return {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_audible_parse_failed",
            "warnings": warnings,
            "audible": {"marketplace": spec["key"], "requestedUrl": requested_url},
            "personalData": {"mode": mode, "privacyMode": privacy_mode},
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "shortCircuit": True,
            },
            "message": str(exc),
        }

    state_path = Path(str(merged.get("stateFile") or "")).expanduser() if merged.get("stateFile") else None
    state = load_state(state_path)
    deal_key = build_deal_key(spec, candidate, store_date)
    if invocation_mode == "scheduled" and state_path and state.get("lastEmittedDealKey") == deal_key:
        return {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_duplicate_scheduled_run",
            "warnings": warnings,
            "audible": candidate,
            "personalData": {"mode": mode, "privacyMode": privacy_mode},
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "dealKey": deal_key,
                "invocationMode": invocation_mode,
                "shortCircuit": True,
            },
            "message": f"Scheduled run already emitted deal {deal_key}.",
        }

    personal_rows: list[dict[str, Any]] = []
    csv_stats: dict[str, Any] = {}
    personal_match: dict[str, Any] = {"matched": False, "ambiguous": False, "effectiveShelf": "", "matches": []}
    freshness_days: int | None = None
    if csv_path:
        try:
            personal_rows, csv_stats = load_goodreads_csv(csv_path, csv_columns)
        except ValueError as exc:
            return {
                "schemaVersion": 1,
                "status": "error",
                "reasonCode": "error_csv_unreadable",
                "warnings": warnings,
                "audible": candidate,
                "personalData": {"mode": mode, "privacyMode": privacy_mode},
                "artifacts": {},
                "metadata": {"marketplace": spec["key"], "dealKey": deal_key},
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "schemaVersion": 1,
                "status": "error",
                "reasonCode": "error_csv_unreadable",
                "warnings": warnings,
                "audible": candidate,
                "personalData": {"mode": mode, "privacyMode": privacy_mode},
                "artifacts": {},
                "metadata": {"marketplace": spec["key"], "dealKey": deal_key},
                "message": f"Could not read Goodreads CSV: {exc}",
            }
        personal_match = classify_personal_match(candidate, personal_rows)
        freshness_days = export_age_days(csv_path, store_date)
        if freshness_days > int(merged.get("freshnessDays") or DEFAULT_FRESHNESS_DAYS):
            last_warning = normalize_space(str(state.get("lastStaleWarningDate") or ""))
            should_warn = invocation_mode != "scheduled"
            if invocation_mode == "scheduled":
                if not last_warning:
                    should_warn = True
                else:
                    try:
                        delta = (store_date - date.fromisoformat(last_warning)).days
                    except Exception:
                        delta = 999
                    should_warn = delta >= 7
            if should_warn:
                warnings.append(
                    f"Your Goodreads export is {freshness_days} days old, so newer reads or shelf changes may be missing."
                )

    if personal_match.get("ambiguous"):
        return {
            "schemaVersion": 1,
            "status": "error",
            "reasonCode": "error_ambiguous_personal_match",
            "warnings": warnings,
            "audible": candidate,
            "personalData": {
                "mode": mode,
                "privacyMode": privacy_mode,
                "exactShelfMatch": "",
                "matchedEntries": personal_match["matches"],
            },
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "dealKey": deal_key,
                "invocationMode": invocation_mode,
            },
            "message": "Conflicting Goodreads CSV shelf states were found for the same book. Clean the CSV / Goodreads shelves for that title and rerun.",
        }

    exact_shelf = str(personal_match.get("effectiveShelf") or "")
    if exact_shelf == "read":
        return {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_already_read",
            "warnings": warnings,
            "audible": candidate,
            "personalData": {
                "mode": mode,
                "privacyMode": privacy_mode,
                "exactShelfMatch": exact_shelf,
                "matchedEntries": personal_match["matches"],
            },
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "dealKey": deal_key,
                "invocationMode": invocation_mode,
                "shortCircuit": True,
            },
            "message": "Your Goodreads CSV already marks this book as read.",
        }

    if exact_shelf == "currently-reading":
        return {
            "schemaVersion": 1,
            "status": "suppress",
            "reasonCode": "suppress_currently_reading",
            "warnings": warnings,
            "audible": candidate,
            "personalData": {
                "mode": mode,
                "privacyMode": privacy_mode,
                "exactShelfMatch": exact_shelf,
                "matchedEntries": personal_match["matches"],
            },
            "artifacts": {},
            "metadata": {
                "marketplace": spec["key"],
                "marketplaceLabel": spec["label"],
                "storeLocalDate": store_date.isoformat(),
                "timezone": spec["timezone"],
                "dealKey": deal_key,
                "invocationMode": invocation_mode,
                "shortCircuit": True,
            },
            "message": "Your Goodreads CSV already marks this book as currently-reading.",
        }

    rated_or_reviewed_entries = [
        row
        for row in personal_rows
        if row.get("myRating", 0) > 0 or normalize_space(str(row.get("myReview") or ""))
    ]
    fit_context = build_fit_context(rated_or_reviewed_entries) if rated_or_reviewed_entries else None
    review_source = build_review_source(rated_or_reviewed_entries) if rated_or_reviewed_entries else None
    context_budget = (
        build_context_budget(rated_or_reviewed_entries, fit_context or build_fit_context([]), review_source, notes_text)
        if csv_path
        else {
            "legacyChars": 0,
            "legacyApproxTokens": 0,
            "fitContextBaseChars": 0,
            "fitContextBaseApproxTokens": 0,
            "reviewSourceRawChars": 0,
            "reviewSourceRawApproxTokens": 0,
            "estimatedReviewSummaryChars": 0,
            "estimatedReviewSummaryApproxTokens": 0,
            "estimatedFinalChars": 0,
            "estimatedFinalApproxTokens": 0,
            "savingsChars": 0,
            "savingsPercent": 0.0,
            "notesChars": len(notes_text),
            "notesApproxTokens": approx_token_count(notes_text),
        }
    )

    artifact_dir = Path(str(merged.get("artifactDir") or default_artifact_dir())).expanduser()
    personal_data = {
        "mode": mode,
        "privacyMode": privacy_mode,
        "allowModelPersonalization": privacy_mode != "minimal" and bool(notes_text or rated_or_reviewed_entries),
        "exactShelfMatch": exact_shelf,
        "matchedEntries": personal_match["matches"],
        "csv": {
            "path": str(csv_path) if csv_path else None,
            "freshnessDays": freshness_days,
            "stats": csv_stats,
            "ratedOrReviewedCount": len(rated_or_reviewed_entries),
            "reviewedCount": int((fit_context or {}).get("reviewCount") or 0),
            "fitContextEntryCount": int((fit_context or {}).get("entryCount") or 0),
            "reviewSourceCount": int((review_source or {}).get("entryCount") or 0),
            "contextBudget": context_budget,
        },
        "notes": {
            "path": notes_file or None,
            "chars": len(notes_text),
            "present": bool(notes_text),
        },
    }
    artifacts = write_artifacts(artifact_dir, candidate, personal_data, fit_context, review_source, notes_text)
    result = {
        "schemaVersion": 1,
        "status": "ready",
        "reasonCode": ready_reason,
        "warnings": warnings,
        "audible": candidate,
        "personalData": personal_data,
        "artifacts": artifacts,
        "metadata": {
            "marketplace": spec["key"],
            "marketplaceLabel": spec["label"],
            "timezone": spec["timezone"],
            "threshold": threshold,
            "dealKey": deal_key,
            "invocationMode": invocation_mode,
            "storeLocalDate": store_date.isoformat(),
            "configPath": str(config_path) if config_path else None,
            "stateFile": str(state_path) if state_path else None,
            "supportedMarketplaces": sorted(SUPPORTED_MARKETPLACES),
        },
        "message": "Preparation complete. The skill runtime can now resolve Goodreads public score and write the final recommendation.",
    }
    runtime_artifacts = write_runtime_contract_artifacts(artifact_dir, result)
    result["artifacts"].update(runtime_artifacts)
    prepare_result_path = artifact_dir / "prepare-result.json"
    result["artifacts"]["prepareResultPath"] = str(prepare_result_path)
    write_json_atomic(prepare_result_path, result)
    return result


def mark_emitted(state_file: Path, deal_key: str, *, stale_warning_date: str | None = None) -> dict[str, Any]:
    state = load_state(state_file)
    state["lastEmittedDealKey"] = deal_key
    if stale_warning_date:
        state["lastStaleWarningDate"] = stale_warning_date
    save_state(state_file, state)
    return {"ok": True, "stateFile": str(state_file), "dealKey": deal_key, "staleWarningDate": stale_warning_date}


def show_csv_headers(export_path: Path) -> dict[str, Any]:
    with export_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {"ok": True, "headers": list(reader.fieldnames or [])}
