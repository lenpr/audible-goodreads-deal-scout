from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable

from .audible_catalog import AudibleCatalogClient, RequestBudgetExceeded, deterministic_shuffle
from .goodreads_csv import effective_shelf, load_goodreads_csv
from .settings import default_config_path, default_storage_dir, load_config, validate_marketplace
from .shared import normalize_author_key, normalize_space, normalized_key, write_json_atomic


DEFAULT_SCAN_ORDER = "newest"
SCAN_ORDERS = {"newest", "csv", "oldest", "random"}
STATUS_ORDER = {
    "discounted": 0,
    "included_with_membership": 1,
    "price_hidden": 2,
    "available_no_discount": 3,
    "price_unknown": 4,
    "needs_review": 5,
    "not_found": 6,
    "lookup_failed": 7,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _error_report(reason_code: str, message: str, *, config_path: Path | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "status": "error",
        "reasonCode": reason_code,
        "generatedAt": _now_iso(),
        "marketplace": "us",
        "csvPath": None,
        "selection": {},
        "requestBudget": {"max": 0, "used": 0, "remaining": 0},
        "counts": {},
        "warnings": [message],
        "results": [],
        "metadata": {"configPath": str(config_path) if config_path else None},
        "error": message,
        "exitCode": 1,
    }


def parse_goodreads_date(raw: str) -> str | None:
    text = normalize_space(raw)
    if not text:
        return None
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def row_identity(row: dict[str, Any]) -> str:
    book_id = normalize_space(str(row.get("bookId") or ""))
    if book_id:
        return f"goodreads:{book_id}"
    title_key = normalized_key(str(row.get("title") or ""), ascii_only=True)
    author_key = normalize_author_key(str(row.get("author") or ""), ascii_only=True)
    return f"title-author:{title_key}|{author_key}"


def goodreads_scan_entry(row: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "rowKey": row_identity(row),
        "csvIndex": index,
        "bookId": normalize_space(str(row.get("bookId") or "")) or None,
        "title": normalize_space(str(row.get("title") or "")),
        "author": normalize_space(str(row.get("author") or "")),
        "averageRating": row.get("averageRating"),
        "dateAdded": parse_goodreads_date(str(row.get("dateAdded") or "")) or normalize_space(str(row.get("dateAdded") or "")) or None,
        "isbn": normalize_space(str(row.get("isbn") or "")) or None,
        "isbn13": normalize_space(str(row.get("isbn13") or "")) or None,
    }


def extract_to_read_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if effective_shelf(row) != "to-read":
            continue
        entry = goodreads_scan_entry(row, index)
        key = str(entry["rowKey"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def _date_sort_value(entry: dict[str, Any]) -> str:
    value = str(entry.get("dateAdded") or "")
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value
    return "0000-00-00"


def select_entries(
    entries: list[dict[str, Any]],
    *,
    scan_order: str,
    seed: str,
    offset: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    if scan_order not in SCAN_ORDERS:
        raise ValueError(f"Unsupported --scan-order '{scan_order}'. Use newest, csv, oldest, or random.")
    ordered = list(entries)
    if scan_order == "newest":
        ordered.sort(key=lambda item: (_date_sort_value(item), str(item.get("title") or "")), reverse=True)
    elif scan_order == "oldest":
        ordered.sort(key=lambda item: (_date_sort_value(item) == "0000-00-00", _date_sort_value(item), str(item.get("title") or "")))
    elif scan_order == "random":
        ordered = deterministic_shuffle(ordered, seed)
    start = max(0, offset)
    end = None if limit is None else start + max(0, limit)
    return ordered[start:end]


def _rating_value(result: dict[str, Any]) -> float:
    value = (result.get("goodreads") or {}).get("averageRating")
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _discount_value(result: dict[str, Any]) -> int:
    value = (result.get("pricing") or {}).get("discountPercent")
    try:
        return int(value or 0)
    except Exception:
        return 0


def rank_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (
            STATUS_ORDER.get(str(item.get("status") or ""), 99),
            -_discount_value(item),
            -_rating_value(item),
            str((item.get("goodreads") or {}).get("dateAdded") or ""),
        ),
    )


def count_results(results: list[dict[str, Any]], *, total_to_read: int, selected_rows: int) -> dict[str, Any]:
    statuses = Counter(str(result.get("status") or "unknown") for result in results)
    return {
        "totalWantToRead": total_to_read,
        "selectedRows": selected_rows,
        "scannedRows": len(results),
        "discounted": statuses.get("discounted", 0),
        "availableNoDiscount": statuses.get("available_no_discount", 0),
        "includedWithMembership": statuses.get("included_with_membership", 0),
        "priceHidden": statuses.get("price_hidden", 0),
        "priceUnknown": statuses.get("price_unknown", 0),
        "needsReview": statuses.get("needs_review", 0),
        "notFound": statuses.get("not_found", 0),
        "lookupFailed": statuses.get("lookup_failed", 0),
        "byStatus": dict(sorted(statuses.items())),
    }


def _price_line(result: dict[str, Any]) -> str:
    pricing = result.get("pricing") or {}
    current = pricing.get("currentPrice")
    list_price = pricing.get("listPrice")
    discount = pricing.get("discountPercent")
    if current is not None and list_price is not None and discount is not None:
        return f"${float(current):.2f} (-{int(discount)}%, list ${float(list_price):.2f})"
    if current is not None:
        return f"${float(current):.2f}"
    return str(pricing.get("pricingStatus") or result.get("status") or "unknown").replace("_", " ")


def _candidate_note_line(result: dict[str, Any]) -> str:
    notes = []
    for item in (result.get("candidateNotes") or [])[:3]:
        title = normalize_space(str(item.get("title") or "untitled"))
        status = normalize_space(str(item.get("matchStatus") or "unknown"))
        reason = normalize_space(str(item.get("matchReason") or ""))
        notes.append(f"{title}: {status}" + (f" ({reason})" if reason else ""))
    return "; ".join(notes)


def render_markdown(report: dict[str, Any], *, include_non_deals: bool = False, verbose: bool = False) -> str:
    counts = report.get("counts") or {}
    budget = report.get("requestBudget") or {}
    lines = [
        "# Discounted Want-to-Read Titles",
        "",
        (
            f"Scanned {counts.get('scannedRows', 0)} of {counts.get('selectedRows', 0)} selected "
            f"Want-to-Read books. Live requests: {budget.get('used', 0)}/{budget.get('max', 0)}."
        ),
        (
            "Summary: "
            f"{counts.get('discounted', 0)} discounted, "
            f"{counts.get('includedWithMembership', 0)} included, "
            f"{counts.get('priceHidden', 0)} hidden price, "
            f"{counts.get('priceUnknown', 0)} unknown price, "
            f"{counts.get('needsReview', 0)} need review, "
            f"{counts.get('notFound', 0)} not found, "
            f"{counts.get('lookupFailed', 0)} failed."
        ),
        "",
    ]
    discounted = [result for result in report.get("results") or [] if result.get("status") == "discounted"]
    if not discounted:
        lines.append("No visible numeric Audible discounts were found in this scan.")
    for index, result in enumerate(discounted[:20], start=1):
        goodreads = result.get("goodreads") or {}
        audible = result.get("audible") or {}
        warnings = ", ".join(result.get("warnings") or [])
        rating = goodreads.get("averageRating")
        rating_text = f"Goodreads {rating}" if rating is not None else "Goodreads rating unavailable"
        lines.extend(
            [
                f"{index}. **{audible.get('title') or goodreads.get('title')}** - {audible.get('author') or goodreads.get('author')}",
                f"   Price: {_price_line(result)}. {rating_text}.",
                f"   Audible: {audible.get('url')}",
            ]
        )
        if warnings:
            lines.append(f"   Warnings: {warnings}")
        if verbose:
            lines.append(f"   Search: {result.get('searchUrl')}")
            lines.append(f"   Match: {result.get('matchReason')}")
            candidate_line = _candidate_note_line(result)
            if candidate_line:
                lines.append(f"   Candidates: {candidate_line}")
        lines.append("")
    if len(discounted) > 20:
        lines.append(f"{len(discounted) - 20} additional discounted titles are in the JSON output.")
        lines.append("")
    if include_non_deals:
        section_specs = (
            ("Needs Review", "needs_review"),
            ("Price Hidden", "price_hidden"),
            ("Included With Membership", "included_with_membership"),
            ("Not Found", "not_found"),
        )
        for heading, status in section_specs:
            items = [result for result in report.get("results") or [] if result.get("status") == status]
            if not items:
                continue
            lines.extend([f"## {heading}", ""])
            for result in items[:10]:
                goodreads = result.get("goodreads") or {}
                audible = result.get("audible") or {}
                label = audible.get("title") or goodreads.get("title")
                author = audible.get("author") or goodreads.get("author")
                line = f"- {label} - {author}: {result.get('matchReason')}"
                if verbose:
                    candidate_line = _candidate_note_line(result)
                    line += f" ({result.get('searchUrl')})"
                    if candidate_line:
                        line += f" Candidates: {candidate_line}"
                lines.append(line)
            if len(items) > 10:
                lines.append(f"- {len(items) - 10} more in JSON output.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _storage_dir_for(config_path: Path, config: dict[str, Any]) -> Path:
    if config_path.name == "config.json":
        return config_path.parent
    artifact_dir = normalize_space(str(config.get("artifactDir") or ""))
    if artifact_dir:
        path = Path(artifact_dir).expanduser()
        if path.name == "current" and path.parent.name == "artifacts":
            return path.parent.parent
    return default_storage_dir()


def _single_book_entry(title: str, author: str) -> dict[str, Any]:
    return {
        "rowKey": "single:" + normalized_key(f"{title} {author}", ascii_only=True),
        "csvIndex": 0,
        "bookId": None,
        "title": normalize_space(title),
        "author": normalize_space(author),
        "averageRating": None,
        "dateAdded": None,
        "isbn": None,
        "isbn13": None,
    }


def scan_want_to_read(
    options: dict[str, Any],
    *,
    fetcher: Callable[[str], tuple[str, str]] | None = None,
) -> tuple[dict[str, Any], str, int]:
    config_path = Path(str(options.get("configPath") or default_config_path())).expanduser().resolve()
    try:
        resolved_config_path, file_config = load_config(config_path)
        config = {**file_config, **{key: value for key, value in options.items() if value is not None}}
        marketplace = normalize_space(str(config.get("audibleMarketplace") or "us")).lower() or "us"
        if marketplace != "us":
            raise ValueError("scan-want-to-read supports only Audible US in v1.")
        validate_marketplace(marketplace)
        scan_order = normalize_space(str(config.get("scanOrder") or DEFAULT_SCAN_ORDER)).lower() or DEFAULT_SCAN_ORDER
        seed = normalize_space(str(config.get("seed") or date.today().isoformat()))
        offset = int(config.get("offset") or 0)
        limit = config.get("limit")
        limit_value = None if limit in (None, "") else int(limit)
        max_requests = int(config.get("maxRequests") or 40)
        request_delay = float(config.get("requestDelay") if config.get("requestDelay") is not None else 1.0)
        min_discount_percent = int(config.get("minDiscountPercent") or 10)
        title = normalize_space(str(config.get("title") or ""))
        author = normalize_space(str(config.get("author") or ""))
        if bool(title) != bool(author):
            raise ValueError("--title and --author must be provided together.")
        csv_path: Path | None = None
        total_to_read = 1
        if title and author:
            entries = [_single_book_entry(title, author)]
        else:
            raw_csv_path = normalize_space(str(config.get("goodreadsCsvPath") or ""))
            if not raw_csv_path:
                return _error_report("error_missing_csv", "Goodreads CSV is required for scan-want-to-read.", config_path=resolved_config_path), "", 1
            csv_path = Path(raw_csv_path).expanduser()
            if not csv_path.exists():
                return _error_report("error_missing_csv", f"Goodreads CSV not found at {csv_path}.", config_path=resolved_config_path), "", 1
            rows, _stats = load_goodreads_csv(csv_path, dict(config.get("csvColumns") or {}))
            entries = extract_to_read_entries(rows)
            total_to_read = len(entries)
        selected = select_entries(entries, scan_order=scan_order, seed=seed, offset=offset, limit=limit_value)
    except Exception as exc:
        report = _error_report("error_invalid_input", str(exc), config_path=config_path)
        return report, "", 1

    storage_dir = _storage_dir_for(resolved_config_path, config)
    cache_dir = storage_dir / "cache" / "audible"
    client = AudibleCatalogClient(
        cache_dir=cache_dir,
        max_requests=max_requests,
        request_delay=request_delay,
        refresh_cache=bool(config.get("refreshCache")),
        no_cache=bool(config.get("noCache")),
        offline_fixtures=Path(str(config["offlineFixtures"])).expanduser() if config.get("offlineFixtures") else None,
        fetcher=fetcher,
    )

    results: list[dict[str, Any]] = []
    status = "completed"
    reason_code = "completed"
    exit_code = 0
    warnings: list[str] = []
    for entry in selected:
        try:
            result = client.search_book(entry, min_discount_percent=min_discount_percent)
        except RequestBudgetExceeded:
            status = "partial"
            reason_code = "request_budget_exhausted"
            exit_code = 2
            warnings.append("Request budget exhausted before all selected rows were scanned.")
            break
        results.append(result)
        if client.should_abort_for_blocks():
            status = "aborted"
            reason_code = "audible_block_circuit_open"
            exit_code = 3
            warnings.append("Audible block-like circuit breaker opened.")
            break
        if client.should_abort_for_ordinary_failures():
            status = "partial"
            reason_code = "ordinary_fetch_failure_limit"
            warnings.append("Stopped early after repeated ordinary network failures.")
            break

    ranked_results = rank_results(results)
    counts = count_results(ranked_results, total_to_read=total_to_read, selected_rows=len(selected))
    report = {
        "schemaVersion": 1,
        "status": status,
        "reasonCode": reason_code,
        "generatedAt": _now_iso(),
        "marketplace": "us",
        "csvPath": str(csv_path) if csv_path else None,
        "selection": {
            "order": scan_order,
            "seed": seed,
            "offset": offset,
            "limit": limit_value,
            "selectedRows": len(selected),
            "totalWantToRead": total_to_read,
        },
        "requestBudget": {
            "max": max_requests,
            "used": client.live_requests,
            "remaining": max(0, max_requests - client.live_requests),
        },
        "counts": counts,
        "warnings": warnings,
        "results": ranked_results,
        "metadata": {
            "configPath": str(resolved_config_path),
            "cacheDir": str(cache_dir),
            "parserVersion": "want-to-read-v1",
        },
        "exitCode": exit_code,
    }
    markdown = render_markdown(
        report,
        include_non_deals=bool(config.get("includeNonDeals")),
        verbose=bool(config.get("verbose")),
    )
    if output_json := config.get("outputJson"):
        write_json_atomic(Path(str(output_json)).expanduser(), report)
    if output_md := config.get("outputMd"):
        from .shared import atomic_write_text

        atomic_write_text(Path(str(output_md)).expanduser(), markdown)
    return report, markdown, exit_code


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
