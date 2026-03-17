from __future__ import annotations

from typing import Any


CONTRACT_ID = "market_snapshot_final"
SCHEMA_NAME = "MarketSnapshot"
SCHEMA_VERSION = "3.0"

REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_name",
    "schema_version",
    "snapshot_id",
    "instrument",
    "trade_date",
    "timestamp",
    "session_context",
    "futures_bar",
    "futures_derived",
    "mtf_derived",
    "opening_range",
    "vix_context",
    "strikes",
    "chain_aggregates",
    "atm_options",
    "iv_derived",
    "session_levels",
)

REQUIRED_BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "session_context": (
        "snapshot_id",
        "timestamp",
        "date",
        "time",
        "minutes_since_open",
        "day_of_week",
        "days_to_expiry",
        "is_expiry_day",
        "session_phase",
    ),
    "futures_bar": ("fut_open", "fut_high", "fut_low", "fut_close", "fut_volume", "fut_oi"),
    "futures_derived": (
        "fut_return_5m",
        "fut_return_15m",
        "fut_return_30m",
        "realized_vol_30m",
        "vol_ratio",
        "fut_volume_ratio",
        "fut_oi_change_30m",
        "ema_9",
        "ema_21",
        "ema_50",
        "vwap",
        "price_vs_vwap",
    ),
    "opening_range": ("orh", "orl", "or_width", "price_vs_orh", "price_vs_orl", "orh_broken", "orl_broken"),
    "vix_context": ("vix_current", "vix_prev_close", "vix_intraday_chg", "vix_regime", "vix_spike_flag"),
    "chain_aggregates": (
        "atm_strike",
        "strike_count",
        "total_ce_oi",
        "total_pe_oi",
        "pcr",
        "pcr_change_30m",
        "max_pain",
        "ce_oi_top_strike",
        "pe_oi_top_strike",
    ),
    "atm_options": (
        "atm_ce_strike",
        "atm_ce_open",
        "atm_ce_high",
        "atm_ce_low",
        "atm_ce_close",
        "atm_ce_volume",
        "atm_ce_oi",
        "atm_ce_oi_change_30m",
        "atm_ce_iv",
        "atm_ce_vol_ratio",
        "atm_pe_strike",
        "atm_pe_open",
        "atm_pe_high",
        "atm_pe_low",
        "atm_pe_close",
        "atm_pe_volume",
        "atm_pe_oi",
        "atm_pe_oi_change_30m",
        "atm_pe_iv",
        "atm_pe_vol_ratio",
    ),
    "iv_derived": ("iv_skew", "iv_skew_dir", "iv_percentile", "iv_regime", "iv_expiry_type"),
    "session_levels": (
        "prev_day_high",
        "prev_day_low",
        "prev_day_close",
        "week_high",
        "week_low",
        "overnight_gap",
        "prev_day_pcr",
        "prev_day_max_pain",
    ),
}

REQUIRED_STRIKE_FIELDS = (
    "strike",
    "ce_ltp",
    "pe_ltp",
    "ce_oi",
    "pe_oi",
    "ce_volume",
    "pe_volume",
    "ce_iv",
    "pe_iv",
    "ce_open",
    "ce_high",
    "ce_low",
    "pe_open",
    "pe_high",
    "pe_low",
)


def _ensure_dict(payload: Any, field_name: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    errors.append(f"{field_name} must be an object")
    return {}


def validate_market_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    work = snapshot if isinstance(snapshot, dict) else {}
    if not isinstance(snapshot, dict):
        errors.append("snapshot must be an object")

    for field_name in REQUIRED_TOP_LEVEL_FIELDS:
        if field_name not in work:
            errors.append(f"missing top-level field: {field_name}")

    schema_name = str(work.get("schema_name") or "").strip()
    schema_version = str(work.get("schema_version") or "").strip()
    if schema_name != SCHEMA_NAME:
        errors.append(f"schema_name={schema_name!r} expected={SCHEMA_NAME!r}")
    if schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version={schema_version!r} expected={SCHEMA_VERSION!r}")

    snapshot_id = str(work.get("snapshot_id") or "").strip()
    instrument = str(work.get("instrument") or "").strip()
    trade_date = str(work.get("trade_date") or "").strip()
    timestamp = str(work.get("timestamp") or "").strip()
    if not snapshot_id:
        errors.append("snapshot_id must be non-empty")
    if not instrument:
        errors.append("instrument must be non-empty")
    if not trade_date:
        errors.append("trade_date must be non-empty")
    if not timestamp:
        errors.append("timestamp must be non-empty")

    for block_name, field_names in REQUIRED_BLOCK_FIELDS.items():
        block = _ensure_dict(work.get(block_name), block_name, errors)
        for field_name in field_names:
            if field_name not in block:
                errors.append(f"{block_name}.{field_name} missing")

    strikes = work.get("strikes")
    if not isinstance(strikes, list):
        errors.append("strikes must be a list")
    else:
        for idx, row in enumerate(strikes):
            if not isinstance(row, dict):
                errors.append(f"strikes[{idx}] must be an object")
                continue
            for field_name in REQUIRED_STRIKE_FIELDS:
                if field_name not in row:
                    errors.append(f"strikes[{idx}].{field_name} missing")

    report = {
        "ok": len(errors) == 0,
        "schema_name": schema_name,
        "schema_version": schema_version,
        "error_count": len(errors),
        "errors": errors,
    }
    if errors and raise_on_error:
        preview = "; ".join(errors[:5])
        more = "" if len(errors) <= 5 else f"; ... (+{len(errors) - 5} more)"
        raise ValueError(f"market snapshot validation failed: {preview}{more}")
    return report


__all__ = [
    "CONTRACT_ID",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "REQUIRED_BLOCK_FIELDS",
    "REQUIRED_STRIKE_FIELDS",
    "validate_market_snapshot",
]
