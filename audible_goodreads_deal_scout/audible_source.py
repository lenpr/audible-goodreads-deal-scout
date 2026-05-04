from __future__ import annotations

import gzip
import html
import json
import re
import shutil
import subprocess
import time
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .constants import AUDIBLE_BLOCK_MARKERS, HTTP_USER_AGENT, PRICE_TOKEN_RE, PROMOTION_MARKERS
from .shared import normalize_space, normalized_key, parse_localized_price, split_author_roles, strip_html, truncate_text


class AudibleBlockedError(RuntimeError):
    pass


class AudibleFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        http_status: int | None = None,
        final_url: str | None = None,
        reason_code: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.backend = backend
        self.http_status = http_status
        self.final_url = final_url
        self.reason_code = reason_code
        self.attempts = list(attempts or [])


class AudibleParseError(RuntimeError):
    pass


class NoActivePromotionError(RuntimeError):
    pass


AUDIBLE_FETCH_HEADERS = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
}
SUPPORTED_AUDIBLE_FETCH_BACKENDS = {"auto", "python", "curl"}
CURL_RECOVERABLE_HTTP_STATUSES = {403, 429, 500, 502, 503, 504}
CURL_META_MARKER = "__AUDIBLE_GOODREADS_DEAL_SCOUT_CURL_META__"


class AudibleFetchResult:
    def __init__(
        self,
        text: str,
        final_url: str,
        *,
        backend: str,
        attempts: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.text = text
        self.final_url = final_url
        self.backend = backend
        self.attempts = list(attempts or [])
        self.warnings = list(warnings or [])

    def __iter__(self):
        yield self.text
        yield self.final_url


def curl_available(curl_bin: str = "curl") -> bool:
    return bool(shutil.which(curl_bin) if "/" not in curl_bin else Path(curl_bin).exists())


def _fetch_reason_code(backend: str, http_status: int | None, exc: Exception | None = None) -> str:
    if backend == "python" and http_status == 503:
        return "http_503_python_fetch_rejected"
    if http_status:
        return f"http_{http_status}_{backend}_fetch_failed"
    if isinstance(exc, URLError):
        return f"url_error_{backend}_fetch_failed"
    return f"{backend}_fetch_failed"


def _attempt_payload(
    *,
    backend: str,
    ok: bool,
    url: str,
    final_url: str | None = None,
    http_status: int | None = None,
    reason_code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "backend": backend,
        "ok": ok,
        "url": url,
    }
    if final_url:
        payload["finalUrl"] = final_url
    if http_status is not None:
        payload["httpStatus"] = http_status
    if reason_code:
        payload["reasonCode"] = reason_code
    if error:
        payload["error"] = error
    return payload


def _raise_blocked(
    message: str,
    *,
    backend: str,
    url: str,
    final_url: str | None = None,
    http_status: int | None = None,
    reason_code: str = "audible_block_marker",
) -> None:
    attempt = _attempt_payload(
        backend=backend,
        ok=False,
        url=url,
        final_url=final_url,
        http_status=http_status,
        reason_code=reason_code,
        error=message,
    )
    blocked = AudibleBlockedError(message)
    blocked.backend = backend  # type: ignore[attr-defined]
    blocked.http_status = http_status  # type: ignore[attr-defined]
    blocked.final_url = final_url  # type: ignore[attr-defined]
    blocked.reason_code = reason_code  # type: ignore[attr-defined]
    blocked.attempts = [attempt]  # type: ignore[attr-defined]
    raise blocked


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


def _validate_audible_response(text: str, url: str, *, backend: str, final_url: str | None = None) -> None:
    lowered = text.lower()
    if any(marker in lowered for marker in AUDIBLE_BLOCK_MARKERS):
        _raise_blocked(
            f"Audible blocked the request for {url}.",
            backend=backend,
            url=url,
            final_url=final_url,
        )


def _fetch_python_once(url: str) -> AudibleFetchResult:
    request = urllib.request.Request(url, headers=AUDIBLE_FETCH_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            text = decode_response_bytes(raw, str(response.headers.get("Content-Encoding") or ""))
            final_url = str(response.geturl() or url)
            _validate_audible_response(text, url, backend="python", final_url=final_url)
            return AudibleFetchResult(
                text,
                final_url,
                backend="python",
                attempts=[
                    _attempt_payload(
                        backend="python",
                        ok=True,
                        url=url,
                        final_url=final_url,
                        http_status=int(getattr(response, "status", 200) or 200),
                    )
                ],
            )
    except HTTPError as exc:
        reason_code = _fetch_reason_code("python", exc.code, exc)
        attempt = _attempt_payload(
            backend="python",
            ok=False,
            url=url,
            final_url=str(exc.geturl() or url),
            http_status=exc.code,
            reason_code=reason_code,
            error=str(exc),
        )
        if exc.code in {403, 429}:
            _raise_blocked(
                f"Audible request blocked with HTTP {exc.code}.",
                backend="python",
                url=url,
                final_url=str(exc.geturl() or url),
                http_status=exc.code,
                reason_code=reason_code,
            )
        raise AudibleFetchError(
            f"Audible request failed for {url}: {exc}",
            backend="python",
            http_status=exc.code,
            final_url=str(exc.geturl() or url),
            reason_code=reason_code,
            attempts=[attempt],
        ) from exc
    except URLError as exc:
        reason_code = _fetch_reason_code("python", None, exc)
        attempt = _attempt_payload(
            backend="python",
            ok=False,
            url=url,
            reason_code=reason_code,
            error=str(exc),
        )
        raise AudibleFetchError(
            f"Audible request failed for {url}: {exc}",
            backend="python",
            reason_code=reason_code,
            attempts=[attempt],
        ) from exc


def _fetch_curl_once(url: str, *, curl_bin: str = "curl") -> AudibleFetchResult:
    if not curl_available(curl_bin):
        raise AudibleFetchError(
            f"Audible curl fallback is unavailable because '{curl_bin}' was not found.",
            backend="curl",
            reason_code="curl_unavailable",
            attempts=[
                _attempt_payload(
                    backend="curl",
                    ok=False,
                    url=url,
                    reason_code="curl_unavailable",
                    error=f"{curl_bin} not found",
                )
            ],
        )
    command = [
        curl_bin,
        "--location",
        "--compressed",
        "--silent",
        "--show-error",
        "--max-time",
        "30",
        "--connect-timeout",
        "10",
    ]
    for key, value in AUDIBLE_FETCH_HEADERS.items():
        command.extend(["-H", f"{key}: {value}"])
    command.extend(["-w", f"\n{CURL_META_MARKER}%{{http_code}}\t%{{url_effective}}", url])
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired as exc:
        reason_code = "curl_timeout"
        raise AudibleFetchError(
            f"Audible curl fallback timed out for {url}.",
            backend="curl",
            reason_code=reason_code,
            attempts=[
                _attempt_payload(
                    backend="curl",
                    ok=False,
                    url=url,
                    reason_code=reason_code,
                    error=str(exc),
                )
            ],
        ) from exc
    except OSError as exc:
        reason_code = "curl_process_failed"
        raise AudibleFetchError(
            f"Audible curl fallback failed for {url}: {exc}",
            backend="curl",
            reason_code=reason_code,
            attempts=[
                _attempt_payload(
                    backend="curl",
                    ok=False,
                    url=url,
                    reason_code=reason_code,
                    error=str(exc),
                )
            ],
        ) from exc
    stdout = proc.stdout or ""
    body, marker, meta = stdout.rpartition(CURL_META_MARKER)
    http_status: int | None = None
    final_url = url
    if marker:
        raw_status, _, raw_final_url = meta.strip().partition("\t")
        try:
            http_status = int(raw_status)
        except ValueError:
            http_status = None
        final_url = normalize_space(raw_final_url) or url
    else:
        body = stdout
    if proc.returncode != 0:
        reason_code = "curl_process_failed"
        attempt = _attempt_payload(
            backend="curl",
            ok=False,
            url=url,
            final_url=final_url,
            http_status=http_status,
            reason_code=reason_code,
            error=(proc.stderr or "").strip() or f"curl exited {proc.returncode}",
        )
        raise AudibleFetchError(
            f"Audible curl fallback failed for {url}: {attempt['error']}",
            backend="curl",
            http_status=http_status,
            final_url=final_url,
            reason_code=reason_code,
            attempts=[attempt],
        )
    if http_status is not None and http_status >= 400:
        reason_code = _fetch_reason_code("curl", http_status)
        attempt = _attempt_payload(
            backend="curl",
            ok=False,
            url=url,
            final_url=final_url,
            http_status=http_status,
            reason_code=reason_code,
            error=f"HTTP {http_status}",
        )
        if http_status in {403, 429}:
            _raise_blocked(
                f"Audible curl fallback blocked with HTTP {http_status}.",
                backend="curl",
                url=url,
                final_url=final_url,
                http_status=http_status,
                reason_code=reason_code,
            )
        raise AudibleFetchError(
            f"Audible curl fallback failed for {url}: HTTP {http_status}",
            backend="curl",
            http_status=http_status,
            final_url=final_url,
            reason_code=reason_code,
            attempts=[attempt],
        )
    _validate_audible_response(body, url, backend="curl", final_url=final_url)
    return AudibleFetchResult(
        body,
        final_url,
        backend="curl",
        attempts=[
            _attempt_payload(
                backend="curl",
                ok=True,
                url=url,
                final_url=final_url,
                http_status=http_status,
            )
        ],
    )


def _fetch_with_retries(
    url: str,
    *,
    backend: str,
    retries: int,
    backoff_seconds: float,
    curl_bin: str,
) -> AudibleFetchResult:
    last_error: Exception | None = None
    attempts: list[dict[str, Any]] = []
    for attempt_index in range(max(0, retries) + 1):
        try:
            result = _fetch_python_once(url) if backend == "python" else _fetch_curl_once(url, curl_bin=curl_bin)
            result.attempts = [*attempts, *result.attempts]
            return result
        except (AudibleFetchError, AudibleBlockedError) as exc:
            last_error = exc
            attempts.extend(list(getattr(exc, "attempts", []) or []))
            if attempt_index >= max(0, retries):
                break
            if backoff_seconds > 0:
                time.sleep(backoff_seconds * (attempt_index + 1))
    if isinstance(last_error, AudibleBlockedError):
        last_error.attempts = attempts  # type: ignore[attr-defined]
        raise last_error
    if isinstance(last_error, AudibleFetchError):
        last_error.attempts = attempts
        raise last_error
    raise AudibleFetchError(f"Audible request failed for {url}: {last_error}", backend=backend, attempts=attempts)


def _can_try_curl_after(error: Exception) -> bool:
    http_status = getattr(error, "http_status", None)
    if http_status in CURL_RECOVERABLE_HTTP_STATUSES:
        return True
    reason_code = normalize_space(str(getattr(error, "reason_code", "") or ""))
    return reason_code.startswith("url_error_") or reason_code in {"python_fetch_failed"}


def fetch_text_with_final_url(
    url: str,
    *,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    backend: str = "auto",
    curl_bin: str = "curl",
) -> AudibleFetchResult:
    normalized_backend = normalize_space(backend).lower() or "auto"
    if normalized_backend not in SUPPORTED_AUDIBLE_FETCH_BACKENDS:
        raise ValueError(
            f"Unsupported Audible fetch backend '{backend}'. Use one of: auto, curl, python."
        )
    if normalized_backend in {"python", "curl"}:
        return _fetch_with_retries(
            url,
            backend=normalized_backend,
            retries=retries,
            backoff_seconds=backoff_seconds,
            curl_bin=curl_bin,
        )
    try:
        return _fetch_with_retries(
            url,
            backend="python",
            retries=retries,
            backoff_seconds=backoff_seconds,
            curl_bin=curl_bin,
        )
    except (AudibleFetchError, AudibleBlockedError) as python_error:
        if not _can_try_curl_after(python_error):
            raise
        python_attempts = list(getattr(python_error, "attempts", []) or [])
        if not curl_available(curl_bin):
            raise AudibleFetchError(
                f"{python_error}; curl fallback unavailable because '{curl_bin}' was not found.",
                backend="auto",
                http_status=getattr(python_error, "http_status", None),
                final_url=getattr(python_error, "final_url", None),
                reason_code=getattr(python_error, "reason_code", None) or "curl_unavailable",
                attempts=[
                    *python_attempts,
                    _attempt_payload(
                        backend="curl",
                        ok=False,
                        url=url,
                        reason_code="curl_unavailable",
                        error=f"{curl_bin} not found",
                    ),
                ],
            ) from python_error
        try:
            curl_result = _fetch_with_retries(
                url,
                backend="curl",
                retries=0,
                backoff_seconds=0,
                curl_bin=curl_bin,
            )
        except (AudibleFetchError, AudibleBlockedError) as curl_error:
            curl_error.attempts = [*python_attempts, *list(getattr(curl_error, "attempts", []) or [])]  # type: ignore[attr-defined]
            raise curl_error from python_error
        http_status = getattr(python_error, "http_status", None)
        reason_code = getattr(python_error, "reason_code", None) or _fetch_reason_code("python", http_status)
        curl_result.attempts = [*python_attempts, *curl_result.attempts]
        curl_result.warnings.append(
            "Python Audible fetch failed"
            + (f" with HTTP {http_status}" if http_status else "")
            + "; recovered with curl fallback."
        )
        if reason_code:
            curl_result.warnings.append(f"Fetch fallback reason: {reason_code}.")
        return curl_result


def _parse_json_ld_blocks(html_text: str) -> list[Any]:
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


def _flatten_json_ld_items(html_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in _parse_json_ld_blocks(html_text):
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict):
                items.append(item)
    return items


def _find_json_ld_product(html_text: str) -> dict[str, Any] | None:
    for item in _flatten_json_ld_items(html_text):
        if item.get("@type") == "Product":
            return item
    return None


def _extract_audible_metadata_blocks(html_text: str) -> list[dict[str, Any]]:
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


def _parse_author_entities(raw: Any) -> list[str]:
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
            values.extend(_parse_author_entities(item))
    return values


def _merge_audible_metadata(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"authors": [], "categories": []}
    for block in blocks:
        if not merged.get("duration") and block.get("duration"):
            merged["duration"] = block.get("duration")
        if not merged.get("releaseDate") and block.get("releaseDate"):
            merged["releaseDate"] = block.get("releaseDate")
        if not merged.get("authors") and block.get("authors"):
            merged["authors"] = _parse_author_entities(block.get("authors"))
        if isinstance(block.get("categories"), list):
            categories = merged.setdefault("categories", [])
            for item in block["categories"]:
                if not isinstance(item, dict):
                    continue
                label = normalize_space(str(item.get("name") or ""))
                if label and label not in categories:
                    categories.append(label)
    return merged


def _extract_json_ld_authors(html_text: str) -> list[str]:
    authors: list[str] = []
    for item in _flatten_json_ld_items(html_text):
        for author in _parse_author_entities(item.get("author")):
            if author and author not in authors:
                authors.append(author)
    return authors


def _parse_audible_summary(html_text: str) -> str:
    match = re.search(r'<adbl-text-block[^>]+slot="summary"[^>]*>(.*?)</adbl-text-block>', html_text, re.I | re.S)
    if match:
        return strip_html(match.group(1))
    return ""


def _is_plausible_genre_label(value: str) -> bool:
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
        if _is_plausible_genre_label(label) and label not in genres:
            genres.append(label)
    return genres


def _parse_audible_author_fallback(html_text: str) -> str:
    matches = re.findall(r'<a href="/author/[^"]+">([^<]+)</a>', html_text, re.I)
    if matches:
        return normalize_space(strip_html(matches[0]))
    match = re.search(r"By:\s*</span>\s*<a[^>]*>([^<]+)</a>", html_text, re.I | re.S)
    if match:
        return normalize_space(strip_html(match.group(1)))
    return ""


def _parse_audible_author_info(html_text: str, metadata: dict[str, Any]) -> tuple[str, str, str]:
    metadata_authors = _parse_author_entities(metadata.get("authors"))
    if metadata_authors:
        author = split_author_roles(metadata_authors[0])
        if author:
            return author, "structured_metadata", "high"
    json_ld_authors = _extract_json_ld_authors(html_text)
    if json_ld_authors:
        author = split_author_roles(json_ld_authors[0])
        if author:
            return author, "json_ld", "high"
    fallback_author = split_author_roles(_parse_audible_author_fallback(html_text))
    if fallback_author:
        return fallback_author, "html_fallback", "low"
    return "", "missing", "missing"


def _parse_release_year(raw_release_date: str | None) -> int | None:
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


def _parse_audible_member_hidden(html_text: str) -> bool:
    lowered = html_text.lower()
    return "member only" in lowered or "logged in to redeem this offer price" in lowered


def _first_price_near_markers(html_text: str) -> float | None:
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


def _parse_list_price(product_json: dict[str, Any], html_text: str) -> float | None:
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


def _parse_isbn_like(product_json: dict[str, Any], metadata: dict[str, Any], html_text: str, field_name: str) -> str:
    for source in (product_json, metadata):
        value = normalize_space(str(source.get(field_name) or ""))
        if value:
            return value
    match = re.search(rf"{field_name}\D+([0-9Xx-]+)", html_text, re.I)
    if match:
        return normalize_space(match.group(1))
    return ""


def parse_audible_deal(html_text: str, final_url: str, requested_url: str) -> dict[str, Any]:
    product_json = _find_json_ld_product(html_text) or {}
    metadata = _merge_audible_metadata(_extract_audible_metadata_blocks(html_text))
    title = normalize_space(str(product_json.get("name") or ""))
    if not title:
        title_match = re.search(r"<h1[^>]*>\s*([^<]+?)\s*</h1>", html_text, re.I | re.S)
        title = normalize_space(strip_html(title_match.group(1) if title_match else ""))
    if not title:
        raise AudibleParseError("failed to parse Audible title")

    author, author_source, author_reliability = _parse_audible_author_info(html_text, metadata)
    if not author:
        raise AudibleParseError("failed to parse Audible author")

    summary = _parse_audible_summary(html_text)
    if not summary:
        raise AudibleParseError("failed to parse Audible summary")

    sale_price = _first_price_near_markers(html_text)
    member_hidden = _parse_audible_member_hidden(html_text)
    if sale_price is None and not member_hidden:
        raise NoActivePromotionError(f"No active daily promotion was found at {requested_url}.")

    runtime = normalize_space(str(metadata.get("duration") or "")) or normalize_space(str(product_json.get("duration") or "")) or "Unknown"
    year = _parse_release_year(str(metadata.get("releaseDate") or "")) or _parse_release_year(str(product_json.get("datePublished") or ""))
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
        "listPrice": _parse_list_price(product_json, html_text),
        "memberHidden": member_hidden,
        "audibleUrl": final_url,
        "isbn": _parse_isbn_like(product_json, metadata, html_text, "isbn"),
        "isbn13": _parse_isbn_like(product_json, metadata, html_text, "isbn13"),
    }
