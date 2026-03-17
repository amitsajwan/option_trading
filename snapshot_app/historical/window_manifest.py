"""Window-manifest helpers for validated snapshot reset workflows."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

DEFAULT_REQUIRED_SCHEMA_VERSION = "3.0"
DEFAULT_MIN_TRADING_DAYS = 150


def window_manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_window_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"window manifest not found: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("window manifest must be a JSON object")
    return payload


def validate_window_manifest(
    manifest: dict[str, Any],
    *,
    formal_run: bool,
    required_schema_version: str = DEFAULT_REQUIRED_SCHEMA_VERSION,
    min_trading_days: int = DEFAULT_MIN_TRADING_DAYS,
    context: str = "window_manifest",
) -> dict[str, Any]:
    required_fields = [
        "window_start",
        "window_end",
        "trading_days",
        "all_days_required_schema",
        "schema_version",
        "generated_at",
        "source_path",
    ]
    missing = [field for field in required_fields if field not in manifest]
    if missing:
        raise ValueError(f"{context}: missing required fields: {', '.join(missing)}")

    window_start = str(manifest.get("window_start") or "").strip()
    window_end = str(manifest.get("window_end") or "").strip()
    if not window_start or not window_end:
        raise ValueError(f"{context}: window_start/window_end must be non-empty")
    try:
        start_d = date.fromisoformat(window_start)
        end_d = date.fromisoformat(window_end)
    except ValueError as exc:
        raise ValueError(f"{context}: invalid window_start/window_end date format") from exc
    if start_d > end_d:
        raise ValueError(f"{context}: window_start must be <= window_end")

    try:
        trading_days = int(manifest.get("trading_days"))
    except Exception as exc:
        raise ValueError(f"{context}: trading_days must be an integer") from exc
    if trading_days < 0:
        raise ValueError(f"{context}: trading_days must be >= 0")

    all_days_required_schema = bool(manifest.get("all_days_required_schema"))
    schema_version = str(manifest.get("schema_version") or "").strip()
    required = str(required_schema_version).strip()
    formal_ready = bool(
        all_days_required_schema and schema_version == required and trading_days >= int(min_trading_days)
    )

    if formal_run and not formal_ready:
        reasons: list[str] = []
        if not all_days_required_schema:
            reasons.append("all_days_required_schema=false")
        if schema_version != required:
            reasons.append(f"schema_version={schema_version!r} expected={required!r}")
        if trading_days < int(min_trading_days):
            reasons.append(f"trading_days={trading_days} < {int(min_trading_days)}")
        raise ValueError(f"{context}: formal run blocked; readiness failed ({'; '.join(reasons)})")

    normalized = dict(manifest)
    normalized.update(
        {
            "window_start": window_start,
            "window_end": window_end,
            "trading_days": trading_days,
            "all_days_required_schema": all_days_required_schema,
            "schema_version": schema_version,
            "required_schema_version": required,
            "min_trading_days_required": int(min_trading_days),
            "formal_ready": formal_ready,
            "exploratory_only": bool(not formal_ready or not formal_run),
            "formal_run": bool(formal_run),
        }
    )
    return normalized


def load_and_validate_window_manifest(
    path: str | Path,
    *,
    formal_run: bool,
    required_schema_version: str = DEFAULT_REQUIRED_SCHEMA_VERSION,
    min_trading_days: int = DEFAULT_MIN_TRADING_DAYS,
    context: str = "window_manifest",
) -> dict[str, Any]:
    manifest = load_window_manifest(path)
    validated = validate_window_manifest(
        manifest,
        formal_run=formal_run,
        required_schema_version=required_schema_version,
        min_trading_days=min_trading_days,
        context=context,
    )
    validated["manifest_path"] = str(Path(path).resolve())
    validated["manifest_hash"] = window_manifest_hash(manifest)
    return validated


def split_boundaries_for_days(
    days: Iterable[str],
    *,
    train_ratio: float = 0.60,
    valid_ratio: float = 0.20,
) -> dict[str, str]:
    ordered = sorted({str(day) for day in days if str(day).strip()})
    if len(ordered) < 3:
        raise ValueError("at least 3 trading days are required to compute 60/20/20 splits")

    n_days = len(ordered)
    train_count = max(1, int(n_days * float(train_ratio)))
    valid_count = max(1, int(n_days * float(valid_ratio)))
    eval_count = n_days - train_count - valid_count
    if eval_count < 1:
        valid_count = max(1, n_days - train_count - 1)
        eval_count = n_days - train_count - valid_count
    if eval_count < 1:
        train_count = max(1, n_days - 2)
        valid_count = 1
        eval_count = n_days - train_count - valid_count
    if eval_count < 1:
        raise ValueError("unable to construct non-empty train/valid/eval splits")

    train_days = ordered[:train_count]
    valid_days = ordered[train_count : train_count + valid_count]
    eval_days = ordered[train_count + valid_count :]
    if not train_days or not valid_days or not eval_days:
        raise ValueError("computed split contains an empty segment")

    return {
        "train_start": train_days[0],
        "train_end": train_days[-1],
        "valid_start": valid_days[0],
        "valid_end": valid_days[-1],
        "eval_start": eval_days[0],
        "eval_end": eval_days[-1],
    }
