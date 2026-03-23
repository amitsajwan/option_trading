from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .market_snapshot_contract import SCHEMA_VERSION as FINAL_SNAPSHOT_SCHEMA_VERSION

CONTRACT_ID = "snapshot_ml_flat"
CONTRACT_FILES_DIR = "snapshot_ml_flat"
SCHEMA_NAME = "SnapshotMLFlat"
SCHEMA_VERSION = FINAL_SNAPSHOT_SCHEMA_VERSION
PRIMARY_KEY = ["trade_date", "timestamp", "snapshot_id"]
REQUIRED_COLUMNS = [
    "trade_date",
    "year",
    "instrument",
    "timestamp",
    "snapshot_id",
    "schema_name",
    "schema_version",
    "build_source",
    "build_run_id",
    "px_fut_open",
    "px_fut_high",
    "px_fut_low",
    "px_fut_close",
    "px_spot_open",
    "px_spot_high",
    "px_spot_low",
    "px_spot_close",
    "ret_1m",
    "ret_3m",
    "ret_5m",
    "ema_9",
    "ema_21",
    "ema_50",
    "ema_9_21_spread",
    "ema_9_slope",
    "ema_21_slope",
    "ema_50_slope",
    "osc_rsi_14",
    "osc_atr_14",
    "osc_atr_ratio",
    "osc_atr_percentile",
    "osc_atr_daily_percentile",
    "vwap_fut",
    "vwap_distance",
    "dist_from_day_high",
    "dist_from_day_low",
    "dist_basis",
    "dist_basis_change_1m",
    "fut_flow_volume",
    "fut_flow_oi",
    "fut_flow_rel_volume_20",
    "fut_flow_volume_accel_1m",
    "fut_flow_oi_change_1m",
    "fut_flow_oi_change_5m",
    "fut_flow_oi_rel_20",
    "fut_flow_oi_zscore_20",
    "opt_flow_atm_strike",
    "opt_flow_rows",
    "opt_flow_ce_oi_total",
    "opt_flow_pe_oi_total",
    "opt_flow_ce_volume_total",
    "opt_flow_pe_volume_total",
    "opt_flow_pcr_oi",
    "pcr_change_5m",
    "pcr_change_15m",
    "opt_flow_atm_call_return_1m",
    "opt_flow_atm_put_return_1m",
    "opt_flow_atm_oi_change_1m",
    "atm_oi_ratio",
    "near_atm_oi_ratio",
    "opt_flow_ce_pe_oi_diff",
    "opt_flow_ce_pe_volume_diff",
    "opt_flow_options_volume_total",
    "opt_flow_rel_volume_20",
    "time_minute_of_day",
    "time_day_of_week",
    "time_minute_index",
    "ctx_opening_range_ready",
    "ctx_opening_range_breakout_up",
    "ctx_opening_range_breakout_down",
    "ctx_dte_days",
    "ctx_is_expiry_day",
    "ctx_is_near_expiry",
    "ctx_is_high_vix_day",
    "ctx_regime_atr_high",
    "ctx_regime_atr_low",
    "ctx_regime_trend_up",
    "ctx_regime_trend_down",
    "ctx_regime_expiry_near",
]
_STRING_FIELDS = {"trade_date", "instrument", "snapshot_id", "schema_name", "schema_version", "build_source", "build_run_id"}
_INTEGER_FIELDS = {
    "year",
    "time_minute_of_day",
    "time_day_of_week",
    "time_minute_index",
    "ctx_opening_range_ready",
    "ctx_opening_range_breakout_up",
    "ctx_opening_range_breakout_down",
    "ctx_dte_days",
    "ctx_is_expiry_day",
    "ctx_is_near_expiry",
    "ctx_is_high_vix_day",
    "ctx_regime_atr_high",
    "ctx_regime_atr_low",
    "ctx_regime_trend_up",
    "ctx_regime_trend_down",
    "ctx_regime_expiry_near",
}
FIELD_TYPES = {
    col: (
        "datetime"
        if col == "timestamp"
        else "string"
        if col in _STRING_FIELDS
        else "integer"
        if col in _INTEGER_FIELDS
        else "number"
    )
    for col in REQUIRED_COLUMNS
}


def _default_contract_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts" / CONTRACT_FILES_DIR


def load_contract_schema(contract_dir: Optional[Path] = None) -> Dict[str, Any]:
    _ = Path(contract_dir) if contract_dir is not None else _default_contract_dir()
    return {
        "contract_id": CONTRACT_ID,
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "primary_key": list(PRIMARY_KEY),
        "required_columns": list(REQUIRED_COLUMNS),
        "field_types": dict(FIELD_TYPES),
        "fields": [{"name": col, "type": FIELD_TYPES[col], "nullable": False} for col in REQUIRED_COLUMNS],
    }


def load_feature_groups(contract_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = Path(contract_dir) if contract_dir is not None else _default_contract_dir()
    return json.loads((base / "feature_groups.json").read_text(encoding="utf-8"))


def load_validation_rules(contract_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = Path(contract_dir) if contract_dir is not None else _default_contract_dir()
    import yaml

    return yaml.safe_load((base / "validation_rules.yaml").read_text(encoding="utf-8"))


def load_legacy_mapping(contract_dir: Optional[Path] = None) -> pd.DataFrame:
    base = Path(contract_dir) if contract_dir is not None else _default_contract_dir()
    return pd.read_csv(base / "legacy_to_flat.csv")


def _stringify_day(value: object) -> str:
    return "" if value is None else str(value).strip()


def _check_required_columns(frame: pd.DataFrame, required: List[str]) -> List[str]:
    missing = [str(col) for col in required if str(col) not in frame.columns]
    return [] if not missing else [f"missing required columns: {','.join(missing)}"]


def _check_removed_columns(frame: pd.DataFrame, rules: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    exact = [str(x) for x in list(rules.get("removed_columns_exact", []))]
    regex_rules = [str(x) for x in list(rules.get("removed_columns_regex", []))]
    exact_found = [col for col in exact if col in frame.columns]
    if exact_found:
        errors.append(f"removed columns present: {','.join(sorted(exact_found))}")

    regex_found: List[str] = []
    compiled = [re.compile(pattern) for pattern in regex_rules]
    for col in frame.columns:
        name = str(col)
        if any(regex.fullmatch(name) for regex in compiled):
            regex_found.append(name)
    if regex_found:
        errors.append(f"removed regex columns present: {','.join(sorted(set(regex_found)))}")
    return errors


def _check_primary_key(frame: pd.DataFrame, key_cols: List[str]) -> List[str]:
    if not key_cols:
        return []
    missing = [col for col in key_cols if col not in frame.columns]
    if missing:
        return [f"primary key columns missing: {','.join(missing)}"]
    dup = frame.duplicated(subset=key_cols, keep=False)
    if bool(dup.any()):
        return [f"primary key duplicate rows found: {int(dup.sum())}"]
    return []


def _check_schema_constants(frame: pd.DataFrame, schema: Dict[str, Any], rules: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    expected_name = str(schema.get("schema_name") or "")
    expected_version = str(schema.get("schema_version") or "")
    if "schema_name" in frame.columns:
        bad = frame["schema_name"].astype(str).str.strip() != expected_name
        if bool(bad.any()):
            errors.append(f"schema_name mismatch rows: {int(bad.sum())} expected={expected_name}")
    if "schema_version" in frame.columns:
        bad = frame["schema_version"].astype(str).str.strip() != expected_version
        if bool(bad.any()):
            errors.append(f"schema_version mismatch rows: {int(bad.sum())} expected={expected_version}")

    allowed_sources = [str(x) for x in list(rules.get("allowed_build_sources", []))]
    if allowed_sources and "build_source" in frame.columns:
        bad = ~frame["build_source"].astype(str).isin(allowed_sources)
        if bool(bad.any()):
            errors.append(f"build_source invalid rows: {int(bad.sum())} allowed={','.join(allowed_sources)}")

    if "timestamp" in frame.columns:
        ts = pd.to_datetime(frame["timestamp"], errors="coerce")
        bad = ts.isna()
        if bool(bad.any()):
            errors.append(f"timestamp parse failures: {int(bad.sum())}")
    return errors


def _numeric_finite_violations(frame: pd.DataFrame, columns: List[str]) -> List[str]:
    violations: List[str] = []
    for col in columns:
        if col not in frame.columns:
            continue
        raw = frame[col]
        non_null = raw.notna()
        if not bool(non_null.any()):
            continue
        numeric = pd.to_numeric(raw, errors="coerce")
        bad_non_numeric = non_null & numeric.isna()
        if bool(bad_non_numeric.any()):
            violations.append(f"{col}: non-numeric={int(bad_non_numeric.sum())}")
            continue
        bad_non_finite = non_null & (~np.isfinite(numeric.to_numpy(dtype=float, copy=False)))
        if bool(bad_non_finite.any()):
            violations.append(f"{col}: non-finite={int(bad_non_finite.sum())}")
    return violations


def _check_tier0(frame: pd.DataFrame, rules: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    tier0 = rules.get("tier0", {}) if isinstance(rules.get("tier0"), dict) else {}
    cols = [str(x) for x in list(tier0.get("columns", []))]
    if not cols:
        return errors

    for col in cols:
        if col in frame.columns:
            nulls = int(frame[col].isna().sum())
            if nulls > 0:
                errors.append(f"tier0 nulls: column={col} rows={nulls}")

    if bool(tier0.get("enforce_finite_numeric", True)):
        non_numeric = {"trade_date", "instrument", "timestamp", "snapshot_id", "schema_name", "schema_version", "build_source", "build_run_id"}
        finite_issues = _numeric_finite_violations(frame, [col for col in cols if col not in non_numeric])
        errors.extend([f"tier0 finite: {item}" for item in finite_issues])
    return errors


def _per_day_completeness(frame: pd.DataFrame, col: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for day, grp in frame.groupby("trade_date", sort=True):
        total = int(len(grp))
        if total <= 0:
            continue
        filled = int(grp[col].notna().sum()) if col in grp.columns else 0
        out[_stringify_day(day)] = float(filled / total)
    return out


def _check_tier1(frame: pd.DataFrame, rules: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    tier1 = rules.get("tier1", {}) if isinstance(rules.get("tier1"), dict) else {}
    cols = [str(x) for x in list(tier1.get("columns", []))]
    threshold = float(tier1.get("per_day_completeness_min", 0.98))
    for col in cols:
        if col not in frame.columns:
            continue
        bad_days = [day for day, completeness in _per_day_completeness(frame, col).items() if completeness < threshold]
        if bad_days:
            errors.append(f"tier1 completeness: column={col} threshold={threshold:.4f} bad_days={','.join(bad_days[:10])}")
    return errors


def _check_tier2(frame: pd.DataFrame, rules: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    tier2 = rules.get("tier2", {}) if isinstance(rules.get("tier2"), dict) else {}
    cols = [str(x) for x in list(tier2.get("columns", []))]
    threshold = float(tier2.get("per_day_completeness_min_after_warmup", 0.95))
    threshold_by_column = {
        str(key): float(value)
        for key, value in dict(tier2.get("per_day_completeness_min_after_warmup_by_column", {})).items()
    }
    warmup = {str(key): int(value) for key, value in dict(tier2.get("warmup_bars_by_column", {})).items()}
    min_sessions = {str(key): int(value) for key, value in dict(tier2.get("min_historical_sessions_by_column", {})).items()}

    if "time_minute_index" not in frame.columns:
        return ["tier2 requires time_minute_index column"]

    continuity_cols = {
        "opt_flow_atm_call_return_1m",
        "opt_flow_atm_put_return_1m",
        "opt_flow_atm_oi_change_1m",
    }
    day_order = {day: idx + 1 for idx, day in enumerate(sorted({_stringify_day(value) for value in frame["trade_date"].tolist()}))}
    for col in cols:
        if col not in frame.columns:
            continue
        warmup_bars = int(warmup.get(col, 0))
        col_threshold = float(threshold_by_column.get(col, threshold))
        min_hist = int(min_sessions.get(col, 1))
        for day, grp in frame.groupby("trade_date", sort=True):
            day_key = _stringify_day(day)
            if int(day_order.get(day_key, 0)) < min_hist:
                continue
            grp_idx = pd.to_numeric(grp["time_minute_index"], errors="coerce")
            eligible = grp_idx >= warmup_bars
            if col in continuity_cols:
                if "opt_flow_atm_strike" not in grp.columns:
                    errors.append(f"tier2 requires opt_flow_atm_strike column for {col}")
                    continue
                atm_strike = pd.to_numeric(grp["opt_flow_atm_strike"], errors="coerce")
                eligible = eligible & atm_strike.notna() & atm_strike.eq(atm_strike.shift(1))
                if "opt_flow_rows" in grp.columns:
                    opt_rows = pd.to_numeric(grp["opt_flow_rows"], errors="coerce")
                    eligible = eligible & (opt_rows > 0.0)
            eligible_count = int(eligible.sum())
            if eligible_count <= 0:
                continue
            filled = int(grp.loc[eligible, col].notna().sum())
            completeness = float(filled / eligible_count)
            if completeness < col_threshold:
                errors.append(
                    "tier2 completeness: "
                    f"column={col} day={day_key} warmup={warmup_bars} eligible={eligible_count} "
                    f"filled={filled} threshold={col_threshold:.4f} actual={completeness:.4f}"
                )
    return errors


def validate_snapshot_ml_flat_frame(
    frame: pd.DataFrame,
    *,
    contract_dir: Optional[Path] = None,
    raise_on_error: bool = True,
) -> Dict[str, Any]:
    schema = load_contract_schema(contract_dir=contract_dir)
    rules = load_validation_rules(contract_dir=contract_dir)

    if frame is None or len(frame) == 0:
        return {"ok": True, "rows": 0, "errors": []}

    work = frame.copy()
    if "trade_date" in work.columns:
        work["trade_date"] = work["trade_date"].astype(str)

    errors: List[str] = []
    errors.extend(_check_required_columns(work, [str(col) for col in list(schema.get("required_columns", []))]))
    errors.extend(_check_removed_columns(work, rules))
    errors.extend(_check_primary_key(work, [str(col) for col in list(schema.get("primary_key", []))]))
    errors.extend(_check_schema_constants(work, schema, rules))
    errors.extend(_check_tier0(work, rules))
    errors.extend(_check_tier1(work, rules))
    errors.extend(_check_tier2(work, rules))

    report = {
        "ok": len(errors) == 0,
        "rows": int(len(work)),
        "error_count": int(len(errors)),
        "errors": errors,
        "schema_name": str(schema.get("schema_name") or ""),
        "schema_version": str(schema.get("schema_version") or ""),
    }
    if errors and raise_on_error:
        preview = "; ".join(errors[:5])
        more = "" if len(errors) <= 5 else f"; ... (+{len(errors) - 5} more)"
        raise ValueError(f"snapshot_ml_flat validation failed: {preview}{more}")
    return report


def validate_snapshot_ml_flat_rows(
    rows: List[Dict[str, Any]],
    *,
    contract_dir: Optional[Path] = None,
    raise_on_error: bool = True,
) -> Dict[str, Any]:
    return validate_snapshot_ml_flat_frame(pd.DataFrame(rows), contract_dir=contract_dir, raise_on_error=raise_on_error)


__all__ = [
    "CONTRACT_ID",
    "CONTRACT_FILES_DIR",
    "load_contract_schema",
    "load_feature_groups",
    "load_validation_rules",
    "load_legacy_mapping",
    "validate_snapshot_ml_flat_frame",
    "validate_snapshot_ml_flat_rows",
]
