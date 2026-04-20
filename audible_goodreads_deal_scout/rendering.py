from __future__ import annotations

from typing import Any


def _core():
    from . import core

    return core


def render_message_layout(
    *,
    header: str,
    title_line: str,
    metadata_lines: list[str],
    description_text: str,
    fit_text: str | None,
    footer_lines: list[str],
    warnings: list[str],
) -> str:
    parts: list[str] = [header, "", title_line, *metadata_lines]
    if description_text:
        parts.extend(["", description_text])
    if fit_text:
        parts.extend(["", fit_text])
    if footer_lines:
        parts.extend(["", *footer_lines])
    if warnings:
        parts.extend(["", "Warnings: " + " ".join(warnings)])
    return "\n".join(parts).strip()


def summary_fit_text(final_result: dict[str, Any]) -> str:
    reason_code = str(final_result.get("reasonCode") or "")
    mapping = {
        "suppress_already_read": "Fit: You marked it as read on Goodreads.",
        "suppress_currently_reading": "Fit: You marked it as currently reading on Goodreads.",
        "suppress_below_goodreads_threshold": "Fit: Goodreads is below your cutoff.",
        "suppress_no_goodreads_match": "Fit: Goodreads could not confirm a matching book.",
        "suppress_no_active_promotion": "Fit: No daily promotion could be confirmed.",
        "suppress_duplicate_scheduled_run": "Fit: This deal was already handled today.",
        "error_goodreads_lookup_failed": "Fit: Goodreads could not be verified right now.",
        "error_audible_fetch_failed": "Fit: Audible could not be verified right now.",
        "error_audible_parse_failed": "Fit: Audible could not be verified right now.",
        "error_audible_blocked": "Fit: Audible could not be verified right now.",
        "error_ambiguous_personal_match": "Fit: Goodreads has conflicting shelf information for this title.",
        "error_csv_unreadable": "Fit: Goodreads data could not be read right now.",
        "error_missing_csv": "Fit: Goodreads data is not available right now.",
    }
    return mapping.get(reason_code, "Fit: This deal could not be verified right now.")


def render_final_message(final_result: dict[str, Any]) -> str:
    core = _core()
    audible = final_result.get("audible") or {}
    metadata = final_result.get("metadata") or {}
    goodreads = final_result.get("goodreads") or {}
    warnings = list(final_result.get("warnings") or [])
    marketplace_label = metadata.get("marketplaceLabel") or f"Audible {str(metadata.get('marketplace') or '').upper()}"
    store_date = core.normalize_space(str(metadata.get("storeLocalDate") or ""))
    header = f"{marketplace_label} Daily Promotion" + (f" — {store_date}" if store_date else "")
    marketplace_key = str(metadata.get("marketplace") or "us")
    title_line = f"{core.bold_visible_text(str(audible.get('title', 'Unknown Title')))} — {audible.get('author', 'Unknown Author')}"
    if audible.get("year"):
        title_line += f" ({audible['year']})"
    metadata_lines = [core.price_display(audible, marketplace_key)]
    if goodreads.get("status") == "resolved" and goodreads.get("averageRating") is not None:
        rating_line = f"Goodreads rating: {goodreads['averageRating']:.2f}"
        ratings_count = goodreads.get("ratingsCount")
        if ratings_count:
            rating_line += f" ({int(ratings_count):,} ratings)"
        metadata_lines.append(rating_line)
    runtime = core.format_runtime(str(audible.get("runtime") or ""))
    if runtime and runtime.lower() != "unknown":
        metadata_lines.append(f"Length: {runtime}")
    genres = [core.normalize_space(str(label)) for label in list(audible.get("genres") or []) if core.normalize_space(str(label))]
    if genres:
        metadata_lines.append(f"Genre: {', '.join(genres)}")
    if final_result["status"] != "recommend":
        metadata_lines.append(f"Reason: {final_result.get('reasonText') or final_result.get('reasonCode')}")
    fit_sentence = core.normalize_space(str(final_result.get("fitSentence") or ""))
    footer_lines: list[str] = []
    if audible.get("audibleUrl"):
        footer_lines.append(f"Audible: {audible['audibleUrl']}")
    if goodreads.get("url"):
        footer_lines.append(f"Goodreads: {goodreads['url']}")
    return render_message_layout(
        header=header,
        title_line=title_line,
        metadata_lines=metadata_lines,
        description_text=core.offer_description(audible),
        fit_text=fit_sentence or None,
        footer_lines=footer_lines,
        warnings=warnings,
    )


def render_delivery_summary_message(final_result: dict[str, Any]) -> str:
    core = _core()
    audible = final_result.get("audible") or {}
    metadata = final_result.get("metadata") or {}
    warnings = list(final_result.get("warnings") or [])
    marketplace_label = metadata.get("marketplaceLabel") or f"Audible {str(metadata.get('marketplace') or '').upper()}"
    store_date = core.normalize_space(str(metadata.get("storeLocalDate") or ""))
    header = f"{marketplace_label} Daily Promotion" + (f" — {store_date}" if store_date else "")
    title_line = f"{core.bold_visible_text(str(audible.get('title', 'Unknown Title')))} — {audible.get('author', 'Unknown Author')}"
    if audible.get("year"):
        title_line += f" ({audible['year']})"
    footer_lines: list[str] = []
    if audible.get("audibleUrl"):
        footer_lines.append(f"Audible: {audible['audibleUrl']}")
    return render_message_layout(
        header=header,
        title_line=title_line,
        metadata_lines=[],
        description_text="",
        fit_text=summary_fit_text(final_result),
        footer_lines=footer_lines,
        warnings=warnings,
    )


def build_delivery_plan(final_result: dict[str, Any], policy: str) -> dict[str, Any]:
    core = _core()
    normalized_policy = core.normalize_delivery_policy(policy)
    status = str(final_result.get("status") or "")
    if normalized_policy == "positive_only":
        if status == "recommend":
            return {
                "policy": normalized_policy,
                "mode": "full",
                "shouldDeliver": True,
                "message": str(final_result.get("message") or ""),
                "skipReason": None,
            }
        return {
            "policy": normalized_policy,
            "mode": "skip",
            "shouldDeliver": False,
            "message": None,
            "skipReason": f"Delivery policy {normalized_policy} skips {status} results.",
        }
    if normalized_policy == "always_full":
        return {
            "policy": normalized_policy,
            "mode": "full",
            "shouldDeliver": True,
            "message": str(final_result.get("message") or ""),
            "skipReason": None,
        }
    if status == "recommend":
        return {
            "policy": normalized_policy,
            "mode": "full",
            "shouldDeliver": True,
            "message": str(final_result.get("message") or ""),
            "skipReason": None,
        }
    return {
        "policy": normalized_policy,
        "mode": "summary",
        "shouldDeliver": True,
        "message": render_delivery_summary_message(final_result),
        "skipReason": None,
    }
