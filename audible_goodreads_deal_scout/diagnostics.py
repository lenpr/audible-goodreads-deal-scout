from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audible_auth import auth_file_status
from .audible_source import (
    SUPPORTED_AUDIBLE_FETCH_BACKENDS,
    AudibleBlockedError,
    AudibleFetchError,
    curl_available,
    fetch_text_with_final_url,
)
from .delivery import build_cron_message, list_cron_jobs
from .settings import default_config_path, default_storage_dir, load_config, skill_root, validate_marketplace
from .shared import normalize_space


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _path_check(path_value: str | None, *, required: bool = False, label: str = "path") -> dict[str, Any]:
    path_text = normalize_space(str(path_value or ""))
    if not path_text:
        return {
            "ok": not required,
            "status": "not_configured",
            "path": None,
            "exists": False,
            "message": f"{label} is not configured.",
        }
    path = Path(path_text).expanduser()
    exists = path.exists()
    return {
        "ok": exists or not required,
        "status": "ok" if exists else "missing",
        "path": str(path),
        "exists": exists,
        "message": f"{label} {'exists' if exists else 'does not exist'}.",
    }


def _wrapper_check() -> dict[str, Any]:
    wrapper = skill_root() / "scripts" / "audible-goodreads-deal-scout.sh"
    return {
        "ok": wrapper.exists(),
        "status": "ok" if wrapper.exists() else "missing",
        "path": str(wrapper),
        "exists": wrapper.exists(),
        "invocation": f"sh {wrapper}",
    }


def _storage_dir_for(config_path: Path, config: dict[str, Any]) -> Path:
    if config_path.name == "config.json":
        return config_path.parent
    artifact_dir = normalize_space(str(config.get("artifactDir") or ""))
    if artifact_dir:
        path = Path(artifact_dir).expanduser()
        if path.name == "current" and path.parent.name == "artifacts":
            return path.parent.parent
    return default_storage_dir()


def _openclaw_check(openclaw_bin: str) -> dict[str, Any]:
    resolved = shutil.which(openclaw_bin) if "/" not in openclaw_bin else (openclaw_bin if Path(openclaw_bin).exists() else None)
    return {
        "ok": bool(resolved),
        "status": "ok" if resolved else "missing",
        "bin": openclaw_bin,
        "resolvedPath": resolved,
    }


def _audible_fetch_backend_check(config: dict[str, Any]) -> dict[str, Any]:
    backend = normalize_space(str(config.get("audibleFetchBackend") or "auto")).lower() or "auto"
    curl_ready = curl_available()
    if backend not in SUPPORTED_AUDIBLE_FETCH_BACKENDS:
        return {
            "ok": False,
            "status": "invalid",
            "backend": backend,
            "curlAvailable": curl_ready,
            "errors": [f"Unsupported audibleFetchBackend '{backend}'. Use auto, python, or curl."],
        }
    if backend == "curl" and not curl_ready:
        return {
            "ok": False,
            "status": "curl_missing",
            "backend": backend,
            "curlAvailable": False,
            "errors": ["audibleFetchBackend is set to curl, but curl was not found on PATH."],
        }
    warnings = []
    if backend == "auto" and not curl_ready:
        warnings.append("curl was not found; prepare cannot use the browser-like fallback if Python fetching is rejected.")
    return {
        "ok": True,
        "status": "ok" if not warnings else "warning",
        "backend": backend,
        "curlAvailable": curl_ready,
        "warnings": warnings,
    }


def _audible_live_fetch_check(config: dict[str, Any]) -> dict[str, Any]:
    marketplace = normalize_space(str(config.get("audibleMarketplace") or "us")).lower() or "us"
    backend = normalize_space(str(config.get("audibleFetchBackend") or "auto")).lower() or "auto"
    try:
        spec = validate_marketplace(marketplace)
        result = fetch_text_with_final_url(
            normalize_space(str(config.get("audibleDealUrl") or spec["dealUrl"])),
            retries=0,
            backend=backend,
        )
    except AudibleBlockedError as exc:
        return {
            "ok": False,
            "status": "blocked",
            "marketplace": marketplace,
            "backend": backend,
            "error": str(exc),
            "attempts": list(getattr(exc, "attempts", []) or []),
        }
    except (AudibleFetchError, ValueError) as exc:
        return {
            "ok": False,
            "status": "fetch_failed",
            "marketplace": marketplace,
            "backend": backend,
            "error": str(exc),
            "reasonCode": getattr(exc, "reason_code", None),
            "attempts": list(getattr(exc, "attempts", []) or []),
        }
    return {
        "ok": True,
        "status": "ok",
        "marketplace": spec["key"],
        "backend": getattr(result, "backend", backend),
        "finalUrl": result.final_url,
        "attempts": result.attempts,
        "warnings": result.warnings,
    }


def _cron_check(
    config_path: Path,
    config: dict[str, Any],
    *,
    openclaw_bin: str,
    check_live_cron: bool,
) -> dict[str, Any]:
    cron_expr = normalize_space(str(config.get("dailyCron") or ""))
    state_file = normalize_space(str(config.get("stateFile") or ""))
    if not cron_expr:
        return {"ok": True, "status": "not_configured", "dailyCron": None, "stateFile": state_file or None}
    try:
        spec = validate_marketplace(str(config.get("audibleMarketplace") or "us"))
    except Exception as exc:
        return {"ok": False, "status": "invalid_marketplace", "dailyCron": cron_expr, "error": str(exc)}
    expected_message = build_cron_message(config_path, Path(state_file).expanduser() if state_file else default_storage_dir() / "state.json")
    result: dict[str, Any] = {
        "ok": bool(state_file),
        "status": "configured" if state_file else "missing_state_file",
        "dailyCron": cron_expr,
        "timezone": spec["timezone"],
        "stateFile": state_file or None,
        "expectedMessage": expected_message,
    }
    if not check_live_cron:
        return result
    try:
        jobs = list_cron_jobs(openclaw_bin)
    except Exception as exc:
        return {**result, "ok": False, "status": "cron_list_failed", "error": str(exc)}
    matches = []
    for job in jobs:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        message = normalize_space(str(payload.get("message") or payload.get("text") or ""))
        if str(config_path) in message or expected_message == message:
            matches.append(
                {
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "enabled": job.get("enabled"),
                    "schedule": job.get("schedule"),
                }
            )
    return {
        **result,
        "ok": bool(matches),
        "status": "matched" if matches else "not_found",
        "matches": matches,
    }


def doctor_report(
    *,
    config_path: Path | None = None,
    auth_path: Path | None = None,
    openclaw_bin: str = "openclaw",
    check_live_cron: bool = False,
    check_audible_fetch: bool = False,
) -> dict[str, Any]:
    resolved_config_path = (config_path or default_config_path()).expanduser().resolve()
    config_exists = resolved_config_path.exists()
    config_parse_error = ""
    config: dict[str, Any] = {}
    if config_exists:
        try:
            parsed_config = json.loads(resolved_config_path.read_text(encoding="utf-8"))
            if isinstance(parsed_config, dict):
                config = parsed_config
            else:
                config_parse_error = "Config file must contain a JSON object."
        except Exception as exc:
            config_parse_error = f"Config file is not readable JSON: {exc}"
    _, loaded_config = load_config(resolved_config_path)
    config = {**loaded_config, **config}
    storage_dir = _storage_dir_for(resolved_config_path, config)
    configured_auth_path = normalize_space(str(auth_path or config.get("audibleAuthPath") or ""))
    checks: dict[str, Any] = {
        "config": {
            "ok": config_exists and not config_parse_error,
            "status": "ok" if config_exists and not config_parse_error else ("invalid" if config_parse_error else "missing"),
            "path": str(resolved_config_path),
            "exists": config_exists,
            "errors": [config_parse_error] if config_parse_error else [],
        },
        "wrapper": _wrapper_check(),
        "openclaw": _openclaw_check(openclaw_bin),
        "audibleFetchBackend": _audible_fetch_backend_check(config),
        "csv": _path_check(config.get("goodreadsCsvPath"), required=False, label="Goodreads CSV"),
        "notes": _path_check(config.get("preferencesPath") or config.get("notesFile"), required=False, label="Preference notes file"),
        "cache": {
            "ok": True,
            "status": "ok" if (storage_dir / "cache" / "audible").exists() else "not_created",
            "path": str(storage_dir / "cache" / "audible"),
            "exists": (storage_dir / "cache" / "audible").exists(),
        },
        "delivery": {
            "ok": bool(config.get("deliveryChannel")) == bool(config.get("deliveryTarget")),
            "status": "configured"
            if config.get("deliveryChannel") and config.get("deliveryTarget")
            else ("partial" if config.get("deliveryChannel") or config.get("deliveryTarget") else "not_configured"),
            "channel": config.get("deliveryChannel"),
            "targetConfigured": bool(config.get("deliveryTarget")),
            "policy": config.get("deliveryPolicy"),
        },
        "cron": _cron_check(
            resolved_config_path,
            config,
            openclaw_bin=openclaw_bin,
            check_live_cron=check_live_cron,
        ),
    }
    if configured_auth_path:
        checks["auth"] = auth_file_status(Path(configured_auth_path), fix_permissions=False)
    else:
        checks["auth"] = {
            "ok": True,
            "status": "not_configured",
            "authPath": None,
            "exists": False,
            "ready": False,
            "warnings": [],
            "errors": [],
        }
    if check_audible_fetch:
        checks["audibleFetchLive"] = _audible_live_fetch_check(config)
    warnings: list[str] = []
    errors: list[str] = []
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        for warning in check.get("warnings") or []:
            warnings.append(f"{name}: {warning}")
        for error in check.get("errors") or []:
            errors.append(f"{name}: {error}")
        if check.get("ok") is False and not check.get("errors"):
            errors.append(f"{name}: {check.get('message') or check.get('status') or 'failed'}")
    ok = not errors
    return {
        "schemaVersion": 1,
        "ok": ok,
        "status": "ok" if ok and not warnings else ("warning" if ok else "error"),
        "reasonCode": "doctor_ok" if ok else "doctor_failed",
        "generatedAt": _now_iso(),
        "configPath": str(resolved_config_path),
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }
