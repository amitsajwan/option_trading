"""Historical Layer-2 snapshot builder."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Deque, Optional

import numpy as np
import pandas as pd

from snapshot_app.core.market_snapshot_contract import (
    SCHEMA_VERSION as FINAL_SNAPSHOT_SCHEMA_VERSION,
    validate_market_snapshot,
)
from snapshot_app.core.market_snapshot import (
    MarketSnapshotState,
    build_market_snapshot,
    prepare_market_snapshot_window,
)
from snapshot_app.core.snapshot_ml_flat_contract import (
    load_contract_schema,
    load_legacy_mapping,
    validate_snapshot_ml_flat_rows,
)

from .parquet_store import ParquetStore

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
IV_HISTORY_MAXLEN = 30_000
CHAIN_HISTORY_MAXLEN = 4_000
OPTION_PRICE_HISTORY_MAXLEN = 4_000
DAY_PROGRESS_EVERY_MINUTES = 120

_EMPTY_CHAIN: dict[str, Any] = {
    "expiry": None,
    "pcr": None,
    "max_pain": None,
    "strikes": [],
    "strike_index": {},
    "ce_volume_total": float("nan"),
    "pe_volume_total": float("nan"),
    "options_rows": float("nan"),
}
OUTPUT_DATASET_SNAPSHOTS = "snapshots"
OUTPUT_DATASET_MARKET_BASE = "market_base"
OUTPUT_DATASET_ML_FLAT = "snapshots_ml_flat"
OUTPUT_DATASET_STAGE1_ENTRY = "stage1_entry_view"
OUTPUT_DATASET_STAGE2_DIRECTION = "stage2_direction_view"
OUTPUT_DATASET_STAGE3_RECIPE = "stage3_recipe_view"
STAGE_OUTPUT_DATASETS = (
    OUTPUT_DATASET_STAGE1_ENTRY,
    OUTPUT_DATASET_STAGE2_DIRECTION,
    OUTPUT_DATASET_STAGE3_RECIPE,
)
CANONICAL_OUTPUT_DATASETS = (
    OUTPUT_DATASET_SNAPSHOTS,
    OUTPUT_DATASET_MARKET_BASE,
)
DERIVED_OUTPUT_DATASETS = (
    OUTPUT_DATASET_ML_FLAT,
    *STAGE_OUTPUT_DATASETS,
)
ML_FLAT_SCHEMA_NAME = "SnapshotMLFlat"
ML_FLAT_SCHEMA_VERSION = FINAL_SNAPSHOT_SCHEMA_VERSION


def _normalize_output_dataset(value: str | None) -> str:
    dataset = str(value or OUTPUT_DATASET_ML_FLAT).strip() or OUTPUT_DATASET_ML_FLAT
    if dataset not in _all_output_datasets():
        raise ValueError(f"unsupported output_dataset: {dataset}")
    return dataset


def _resolve_write_dataset(value: str | None) -> str:
    return _normalize_output_dataset(value)


def _default_build_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@lru_cache(maxsize=1)
def _load_ml_flat_mapping() -> tuple[dict[str, str], set[str]]:
    rename: dict[str, str] = {}
    removed: set[str] = set()
    mapping = load_legacy_mapping()
    for row in mapping.to_dict("records"):
        legacy = str((row or {}).get("legacy_name") or "").strip()
        new_name = str((row or {}).get("new_name") or "").strip()
        is_removed = str((row or {}).get("is_removed") or "").strip().lower() == "true"
        if is_removed and legacy:
            removed.add(legacy)
            continue
        if legacy and new_name:
            rename[legacy] = new_name
    return rename, removed


@lru_cache(maxsize=1)
def _load_ml_flat_required_columns() -> list[str]:
    payload = load_contract_schema()
    cols = [str(x) for x in list(payload.get("required_columns", []))]
    if not cols:
        raise ValueError("snapshot_ml_flat schema has no required_columns")
    return cols


def _safe_num_series(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce")


class MissingInputsError(RuntimeError):
    """Raised when a day cannot be processed due to incomplete upstream parquet."""


@dataclass
class IVStateCarrier:
    """Carry state across days so derived metrics remain realistic."""

    iv_history_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=IV_HISTORY_MAXLEN))
    iv_history_non_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=IV_HISTORY_MAXLEN))
    chain_history: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=CHAIN_HISTORY_MAXLEN))
    option_price_history: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=OPTION_PRICE_HISTORY_MAXLEN))

    def clone(self) -> "IVStateCarrier":
        cloned = IVStateCarrier()
        cloned.iv_history_expiry = deque(self.iv_history_expiry, maxlen=IV_HISTORY_MAXLEN)
        cloned.iv_history_non_expiry = deque(self.iv_history_non_expiry, maxlen=IV_HISTORY_MAXLEN)
        cloned.chain_history = deque((dict(item) for item in self.chain_history), maxlen=CHAIN_HISTORY_MAXLEN)
        cloned.option_price_history = deque(
            (dict(item) for item in self.option_price_history),
            maxlen=OPTION_PRICE_HISTORY_MAXLEN,
        )
        return cloned

    def seed_state(self, state: MarketSnapshotState) -> None:
        for value in self.iv_history_expiry:
            state.iv_history_expiry.append(value)
        for value in self.iv_history_non_expiry:
            state.iv_history_non_expiry.append(value)
        for item in self.chain_history:
            state.chain_history.append(item)
        if hasattr(state, "option_price_history"):
            for item in self.option_price_history:
                state.option_price_history.append(item)

    def absorb_state(self, state: MarketSnapshotState) -> None:
        for value in state.iv_history_expiry:
            self.iv_history_expiry.append(value)
        for value in state.iv_history_non_expiry:
            self.iv_history_non_expiry.append(value)
        for item in state.chain_history:
            self.chain_history.append(item)
        if hasattr(state, "option_price_history"):
            for item in state.option_price_history:
                self.option_price_history.append(item)


def _new_iv_diag() -> dict[str, int]:
    return {
        "minutes": 0,
        "ce_iv_non_null": 0,
        "pe_iv_non_null": 0,
        "ce_iv_from_feed": 0,
        "pe_iv_from_feed": 0,
        "ce_iv_from_solver": 0,
        "pe_iv_from_solver": 0,
        "ce_iv_solver_failed": 0,
        "pe_iv_solver_failed": 0,
        "ce_iv_unexpected_missing": 0,
        "pe_iv_unexpected_missing": 0,
    }


def _canonical_contract_validation_metadata(validate_ml_flat_contract: bool) -> dict[str, Any]:
    return {
        "contract_validation_requested": bool(validate_ml_flat_contract),
        "contract_validation_enabled": False,
        "contract_validation_scope": "canonical_market_snapshot_only",
        "contract_validation_note": (
            "Canonical MarketSnapshot validation is always enforced during snapshot builds. "
            "validate_ml_flat_contract only applies once derived SnapshotMLFlat rows are built."
        ),
    }


def _all_output_datasets() -> tuple[str, ...]:
    return (
        *CANONICAL_OUTPUT_DATASETS,
        *DERIVED_OUTPUT_DATASETS,
    )


def _chunk_out_dir(
    *,
    out_base: Path,
    output_dataset: str,
    year: int,
    partition_key: str | None,
) -> Path:
    out_dir = out_base / _resolve_write_dataset(output_dataset) / f"year={year}"
    part = str(partition_key or "").strip()
    if part:
        out_dir = out_dir / f"chunk={part}"
    return out_dir


def _is_finite_number(value: Any) -> bool:
    out = pd.to_numeric(value, errors="coerce")
    return bool(pd.notna(out) and np.isfinite(float(out)))


def _normalize_iv_input(value: Any) -> Optional[float]:
    out = pd.to_numeric(value, errors="coerce")
    if pd.isna(out):
        return None
    iv = float(out)
    if not np.isfinite(iv):
        return None
    if iv > 3.0:
        iv = iv / 100.0
    if iv <= 0.0:
        return None
    return float(iv)


def _find_atm_row(chain: dict[str, Any], atm_strike: Any) -> Optional[dict[str, Any]]:
    atm = pd.to_numeric(atm_strike, errors="coerce")
    if pd.isna(atm):
        return None
    strike_index = chain.get("strike_index")
    if isinstance(strike_index, dict):
        cached = strike_index.get(int(round(float(atm))))
        if isinstance(cached, dict):
            return cached
    strikes = chain.get("strikes")
    if not isinstance(strikes, list):
        return None
    atm_i = int(round(float(atm)))
    for row in strikes:
        if not isinstance(row, dict):
            continue
        strike = pd.to_numeric(row.get("strike"), errors="coerce")
        if pd.isna(strike):
            continue
        if int(round(float(strike))) == atm_i:
            return row
    return None


def _merge_iv_diag(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _update_iv_diag(iv_diag: dict[str, int], snapshot: dict[str, Any], chain: dict[str, Any]) -> None:
    iv_diag["minutes"] = int(iv_diag.get("minutes", 0)) + 1
    atm_options = snapshot.get("atm_options")
    chain_agg = snapshot.get("chain_aggregates")
    if not isinstance(atm_options, dict) or not isinstance(chain_agg, dict):
        return

    atm_row = _find_atm_row(chain, chain_agg.get("atm_strike"))
    ce_feed = _normalize_iv_input(atm_row.get("ce_iv")) if isinstance(atm_row, dict) else None
    pe_feed = _normalize_iv_input(atm_row.get("pe_iv")) if isinstance(atm_row, dict) else None
    ce_final_ok = _is_finite_number(atm_options.get("atm_ce_iv"))
    pe_final_ok = _is_finite_number(atm_options.get("atm_pe_iv"))

    if ce_final_ok:
        iv_diag["ce_iv_non_null"] = int(iv_diag.get("ce_iv_non_null", 0)) + 1
        if ce_feed is None:
            iv_diag["ce_iv_from_solver"] = int(iv_diag.get("ce_iv_from_solver", 0)) + 1
        else:
            iv_diag["ce_iv_from_feed"] = int(iv_diag.get("ce_iv_from_feed", 0)) + 1
    else:
        if ce_feed is None:
            iv_diag["ce_iv_solver_failed"] = int(iv_diag.get("ce_iv_solver_failed", 0)) + 1
        else:
            iv_diag["ce_iv_unexpected_missing"] = int(iv_diag.get("ce_iv_unexpected_missing", 0)) + 1

    if pe_final_ok:
        iv_diag["pe_iv_non_null"] = int(iv_diag.get("pe_iv_non_null", 0)) + 1
        if pe_feed is None:
            iv_diag["pe_iv_from_solver"] = int(iv_diag.get("pe_iv_from_solver", 0)) + 1
        else:
            iv_diag["pe_iv_from_feed"] = int(iv_diag.get("pe_iv_from_feed", 0)) + 1
    else:
        if pe_feed is None:
            iv_diag["pe_iv_solver_failed"] = int(iv_diag.get("pe_iv_solver_failed", 0)) + 1
        else:
            iv_diag["pe_iv_unexpected_missing"] = int(iv_diag.get("pe_iv_unexpected_missing", 0)) + 1


def _ts_key(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _max_pain_np(strike_arr: np.ndarray, ce_oi_arr: np.ndarray, pe_oi_arr: np.ndarray) -> Optional[int]:
    """Vectorized max-pain calculator."""
    if len(strike_arr) == 0:
        return None
    diff = strike_arr[:, None] - strike_arr[None, :]
    ce_pain = (np.maximum(diff, 0.0) * ce_oi_arr[None, :]).sum(axis=1)
    pe_pain = (np.maximum(-diff, 0.0) * pe_oi_arr[None, :]).sum(axis=1)
    return int(round(float(strike_arr[np.argmin(ce_pain + pe_pain)])))


def _build_all_chains(options_day: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Build chain payloads for all day timestamps in a single grouped pass."""
    if options_day is None or len(options_day) == 0:
        return {}

    work = options_day.copy()
    work["timestamp"] = pd.to_datetime(work.get("timestamp"), errors="coerce")
    work = work.dropna(subset=["timestamp"])
    if len(work) == 0:
        return {}

    for col in ("strike", "open", "high", "low", "close", "oi", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    if "iv" in work.columns:
        work["iv"] = pd.to_numeric(work["iv"], errors="coerce")
    work["option_type"] = work.get("option_type", "").astype(str).str.upper().str.strip()
    work = work.dropna(subset=["strike"])
    if len(work) == 0:
        return {}

    work["_ts"] = work["timestamp"].dt.floor("min").dt.strftime("%Y-%m-%d %H:%M:%S")

    expiry_map: dict[str, str] = {}
    if "expiry_str" in work.columns:
        exp = work.dropna(subset=["expiry_str"]).groupby("_ts")["expiry_str"].first()
        expiry_map = exp.astype(str).str.strip().str.upper().to_dict()

    agg_spec: dict[str, Any] = {"ltp": ("close", "last"), "oi": ("oi", "sum"), "volume": ("volume", "sum")}
    if "open" in work.columns:
        agg_spec["open"] = ("open", "first")
    if "high" in work.columns:
        agg_spec["high"] = ("high", "max")
    if "low" in work.columns:
        agg_spec["low"] = ("low", "min")
    if "iv" in work.columns:
        agg_spec["iv"] = ("iv", "last")

    agg = work.groupby(["_ts", "strike", "option_type"], sort=True).agg(**agg_spec).reset_index()
    ce = agg[agg["option_type"] == "CE"].drop(columns="option_type")
    pe = agg[agg["option_type"] == "PE"].drop(columns="option_type")
    combined = ce.set_index(["_ts", "strike"]).join(pe.set_index(["_ts", "strike"]), how="outer", lsuffix="_ce", rsuffix="_pe").reset_index()
    combined = combined.rename(
        columns={
            "ltp_ce": "ce_ltp",
            "ltp_pe": "pe_ltp",
            "oi_ce": "ce_oi",
            "oi_pe": "pe_oi",
            "volume_ce": "ce_volume",
            "volume_pe": "pe_volume",
            "open_ce": "ce_open",
            "open_pe": "pe_open",
            "high_ce": "ce_high",
            "high_pe": "pe_high",
            "low_ce": "ce_low",
            "low_pe": "pe_low",
            "iv_ce": "ce_iv",
            "iv_pe": "pe_iv",
        }
    )

    for col in ("ce_oi", "ce_volume", "pe_oi", "pe_volume"):
        if col not in combined.columns:
            combined[col] = 0.0
        else:
            combined[col] = combined[col].fillna(0.0)

    for col in ("ce_ltp", "pe_ltp", "ce_iv", "pe_iv", "ce_open", "ce_high", "ce_low", "pe_open", "pe_high", "pe_low"):
        if col not in combined.columns:
            combined[col] = np.nan

    def _f_or_none(value: Any) -> Optional[float]:
        out = pd.to_numeric(value, errors="coerce")
        if pd.isna(out):
            return None
        return float(out)

    out: dict[str, dict[str, Any]] = {}
    for ts_value, grp in combined.groupby("_ts", sort=False):
        ts = str(ts_value)
        strike_arr = grp["strike"].to_numpy(dtype=float)
        ce_oi_arr = grp["ce_oi"].to_numpy(dtype=float)
        pe_oi_arr = grp["pe_oi"].to_numpy(dtype=float)

        max_pain = _max_pain_np(strike_arr, ce_oi_arr, pe_oi_arr)
        total_ce = float(ce_oi_arr.sum())
        total_pe = float(pe_oi_arr.sum())
        pcr = (total_pe / total_ce) if total_ce > 0 else None

        strikes = [
            {
                "strike": float(row.strike),
                "ce_ltp": _f_or_none(row.ce_ltp),
                "pe_ltp": _f_or_none(row.pe_ltp),
                "ce_oi": float(row.ce_oi),
                "pe_oi": float(row.pe_oi),
                "ce_volume": float(row.ce_volume),
                "pe_volume": float(row.pe_volume),
                "ce_iv": _f_or_none(row.ce_iv),
                "pe_iv": _f_or_none(row.pe_iv),
                "ce_open": _f_or_none(row.ce_open),
                "ce_high": _f_or_none(row.ce_high),
                "ce_low": _f_or_none(row.ce_low),
                "pe_open": _f_or_none(row.pe_open),
                "pe_high": _f_or_none(row.pe_high),
                "pe_low": _f_or_none(row.pe_low),
            }
            for row in grp.itertuples(index=False)
        ]

        strike_index = {
            int(round(float(row["strike"]))): row
            for row in strikes
            if pd.notna(pd.to_numeric(row.get("strike"), errors="coerce"))
        }
        out[ts] = {
            "expiry": expiry_map.get(ts),
            "pcr": pcr,
            "max_pain": max_pain,
            "strikes": strikes,
            "strike_index": strike_index,
            "ce_volume_total": float(grp["ce_volume"].sum()),
            "pe_volume_total": float(grp["pe_volume"].sum()),
            "options_rows": float(len(strikes)),
        }
    return out


def _chain_totals(chain: dict[str, Any]) -> tuple[float, float, float]:
    cached_ce = pd.to_numeric(chain.get("ce_volume_total"), errors="coerce")
    cached_pe = pd.to_numeric(chain.get("pe_volume_total"), errors="coerce")
    cached_rows = pd.to_numeric(chain.get("options_rows"), errors="coerce")
    if pd.notna(cached_ce) and pd.notna(cached_pe) and pd.notna(cached_rows):
        return float(cached_ce), float(cached_pe), float(cached_rows)
    strikes = chain.get("strikes")
    if not isinstance(strikes, list) or not strikes:
        return float("nan"), float("nan"), float("nan")
    ce_volume_total = float(
        np.nansum([pd.to_numeric((row or {}).get("ce_volume"), errors="coerce") for row in strikes if isinstance(row, dict)])
    )
    pe_volume_total = float(
        np.nansum([pd.to_numeric((row or {}).get("pe_volume"), errors="coerce") for row in strikes if isinstance(row, dict)])
    )
    rows = float(len(strikes))
    return ce_volume_total, pe_volume_total, rows


def _build_spot_map(
    spot_day: pd.DataFrame,
    *,
    fut_timestamps: pd.Series | list[Any] | None = None,
) -> dict[str, dict[str, float]]:
    if spot_day is None or len(spot_day) == 0:
        return {}

    work = spot_day.copy()
    work["timestamp"] = pd.to_datetime(work.get("timestamp"), errors="coerce")
    work = work.dropna(subset=["timestamp"])
    if len(work) == 0:
        return {}

    for col in ("open", "high", "low", "close"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
        else:
            work[col] = np.nan
    work = work.sort_values("timestamp").reset_index(drop=True)
    spot_frame = work.loc[:, ["timestamp", "open", "high", "low", "close"]].copy()

    if fut_timestamps is not None:
        fut_ts = pd.to_datetime(pd.Series(list(fut_timestamps)), errors="coerce")
        fut_frame = pd.DataFrame({"timestamp": fut_ts}).dropna(subset=["timestamp"])
        fut_frame = fut_frame.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
        grouped = pd.merge_asof(
            fut_frame,
            spot_frame.sort_values("timestamp"),
            on="timestamp",
            direction="backward",
        )
    else:
        grouped = spot_frame.copy()

    grouped["_ts"] = grouped["timestamp"].dt.floor("min").dt.strftime("%Y-%m-%d %H:%M:%S")
    grouped = grouped.groupby("_ts", sort=False).last().reset_index()

    def _num(value: Any) -> float:
        out = pd.to_numeric(value, errors="coerce")
        return float(out) if pd.notna(out) else float("nan")

    out: dict[str, dict[str, float]] = {}
    for row in grouped.to_dict("records"):
        out[str(row.get("_ts") or "")] = {
            "spot_open": _num(row.get("open")),
            "spot_high": _num(row.get("high")),
            "spot_low": _num(row.get("low")),
            "spot_close": _num(row.get("close")),
        }
    return out


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _compute_daily_atr_percentile(fut_window: pd.DataFrame, trade_date: str) -> float:
    if fut_window is None or len(fut_window) == 0:
        return float("nan")

    work = fut_window.copy()
    work["trade_date"] = work.get("trade_date", "").astype(str)
    for col in ("high", "low", "close"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    daily = (
        work.groupby("trade_date", sort=True)
        .agg(
            day_high=("high", "max"),
            day_low=("low", "min"),
            day_close=("close", "last"),
        )
        .reset_index()
    )
    if len(daily) == 0 or trade_date not in set(daily["trade_date"].astype(str)):
        return float("nan")

    prev_close = daily["day_close"].shift(1)
    daily_tr = pd.concat(
        [
            (daily["day_high"] - daily["day_low"]).abs(),
            (daily["day_high"] - prev_close).abs(),
            (daily["day_low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    daily_atr = daily_tr.ewm(alpha=1.0 / 14.0, min_periods=1, adjust=False).mean()
    daily["atr_daily_percentile"] = daily_atr.expanding(min_periods=5).rank(pct=True)
    current = daily.loc[daily["trade_date"].astype(str) == str(trade_date), "atr_daily_percentile"]
    if len(current) == 0:
        return float("nan")
    value = pd.to_numeric(current.iloc[-1], errors="coerce")
    return float(value) if pd.notna(value) else float("nan")


def _preload_futures_windows(
    *,
    store: ParquetStore,
    history_calendar_days: list[str],
    execution_days: list[str],
    futures_window_days_by_day: dict[str, list[str]],
) -> dict[str, pd.DataFrame]:
    required_days: set[str] = set()
    for day in execution_days:
        required_days.update(futures_window_days_by_day.get(str(day), []))
    ordered_required_days = [str(day) for day in history_calendar_days if str(day) in required_days]
    if not ordered_required_days:
        return {}

    full_futures = store.futures_window_for_days(ordered_required_days)
    if len(full_futures) == 0:
        return {}

    by_day = {
        str(trade_date): frame.sort_values("timestamp").reset_index(drop=True)
        for trade_date, frame in full_futures.groupby("trade_date", sort=False)
    }
    window_cache: dict[str, pd.DataFrame] = {}
    for day in execution_days:
        window_days = futures_window_days_by_day.get(str(day), [])
        frames = [by_day[current] for current in window_days if current in by_day]
        if not frames:
            continue
        window_cache[str(day)] = pd.concat(frames, axis=0, ignore_index=True)
    return window_cache


def _project_rows_to_ml_flat(
    rows: list[dict[str, Any]],
    *,
    build_source: str | None = None,
    build_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return []

    _, removed_cols = _load_ml_flat_mapping()
    required_cols = _load_ml_flat_required_columns()

    work = pd.DataFrame(rows).copy()
    work["timestamp"] = pd.to_datetime(work.get("timestamp"), errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if len(work) == 0:
        return []
    drop_cols = [col for col in removed_cols if col in work.columns]
    if drop_cols:
        work = work.drop(columns=drop_cols)

    out = pd.DataFrame(index=work.index)

    def num(*names: str) -> pd.Series:
        for name in names:
            if name in work.columns:
                return _safe_num_series(work, name)
        return pd.Series(np.nan, index=work.index, dtype=float)

    def text(*names: str) -> pd.Series:
        for name in names:
            if name in work.columns:
                return work[name].astype(str)
        return pd.Series("", index=work.index, dtype=object)

    # Metadata.
    out["trade_date"] = text("trade_date")
    out["year"] = num("year")
    out["instrument"] = text("instrument")
    out["timestamp"] = work["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    out["snapshot_id"] = text("snapshot_id")
    out["schema_name"] = ML_FLAT_SCHEMA_NAME
    out["schema_version"] = ML_FLAT_SCHEMA_VERSION
    if build_source is not None:
        out["build_source"] = str(build_source)
    elif "build_source" in work.columns:
        out["build_source"] = (
            work["build_source"]
            .fillna("")
            .astype(str)
            .replace({"": "historical", "nan": "historical", "None": "historical"})
        )
    else:
        out["build_source"] = "historical"
    if build_run_id is not None:
        out["build_run_id"] = str(build_run_id)
    elif "build_run_id" in work.columns:
        default_run_id = _default_build_run_id()
        out["build_run_id"] = (
            work["build_run_id"]
            .fillna("")
            .astype(str)
            .replace({"": default_run_id, "nan": default_run_id, "None": default_run_id})
        )
    else:
        out["build_run_id"] = _default_build_run_id()

    # Base price fields.
    out["px_fut_open"] = num("px_fut_open", "fut_open")
    out["px_fut_high"] = num("px_fut_high", "fut_high")
    out["px_fut_low"] = num("px_fut_low", "fut_low")
    out["px_fut_close"] = num("px_fut_close", "fut_close")
    out["px_spot_open"] = num("px_spot_open", "spot_open")
    out["px_spot_high"] = num("px_spot_high", "spot_high")
    out["px_spot_low"] = num("px_spot_low", "spot_low")
    out["px_spot_close"] = num("px_spot_close", "spot_close")

    close = out["px_fut_close"].astype(float)
    high = out["px_fut_high"].astype(float)
    low = out["px_fut_low"].astype(float)

    # Returns.
    ret_1m = num("ret_1m")
    ret_3m = num("ret_3m")
    ret_5m = num("ret_5m")
    out["ret_1m"] = ret_1m.where(ret_1m.notna(), close.pct_change(1, fill_method=None))
    out["ret_3m"] = ret_3m.where(ret_3m.notna(), close.pct_change(3, fill_method=None))
    out["ret_5m"] = ret_5m.where(ret_5m.notna(), close.pct_change(5, fill_method=None))

    # EMA and trend.
    ema_9 = close.ewm(span=9, adjust=False).mean()
    ema_21 = close.ewm(span=21, adjust=False).mean()
    ema_50 = close.ewm(span=50, adjust=False).mean()
    ema_9_raw = num("ema_9")
    ema_21_raw = num("ema_21")
    ema_50_raw = num("ema_50")
    out["ema_9"] = ema_9_raw.where(ema_9_raw.notna(), ema_9)
    out["ema_21"] = ema_21_raw.where(ema_21_raw.notna(), ema_21)
    out["ema_50"] = ema_50_raw.where(ema_50_raw.notna(), ema_50)
    ema_9_21_spread_raw = num("ema_9_21_spread")
    out["ema_9_21_spread"] = ema_9_21_spread_raw.where(ema_9_21_spread_raw.notna(), out["ema_9"] - out["ema_21"])
    ema_9_slope_raw = num("ema_9_slope")
    ema_21_slope_raw = num("ema_21_slope")
    ema_50_slope_raw = num("ema_50_slope")
    close_denom = close.replace(0.0, np.nan)
    out["ema_9_slope"] = ema_9_slope_raw.where(ema_9_slope_raw.notna(), out["ema_9"].diff() / close_denom)
    out["ema_21_slope"] = ema_21_slope_raw.where(ema_21_slope_raw.notna(), out["ema_21"].diff() / close_denom)
    out["ema_50_slope"] = ema_50_slope_raw.where(ema_50_slope_raw.notna(), out["ema_50"].diff() / close_denom)

    # Oscillators and volatility.
    rsi_legacy = num("osc_rsi_14", "rsi_14_1m")
    if not bool(rsi_legacy.notna().any()):
        rsi_legacy = num("rsi_14")
    out["osc_rsi_14"] = rsi_legacy.where(rsi_legacy.notna(), _compute_rsi(close, period=14))

    atr_legacy = num("osc_atr_14", "atr_14_1m")
    if not bool(atr_legacy.notna().any()):
        atr_legacy = num("atr_14")
    out["osc_atr_14"] = atr_legacy.where(atr_legacy.notna(), _compute_atr(high, low, close, period=14))
    atr_ratio_legacy = num("osc_atr_ratio", "atr_ratio")
    out["osc_atr_ratio"] = atr_ratio_legacy.where(
        atr_ratio_legacy.notna(),
        out["osc_atr_14"] / close.replace(0.0, np.nan),
    )
    atr_pct_legacy = num("osc_atr_percentile", "atr_percentile")
    atr_daily_base = num("atr_daily_percentile")
    out["osc_atr_percentile"] = atr_pct_legacy.where(
        atr_pct_legacy.notna(),
        atr_daily_base.where(
            atr_daily_base.notna(),
            out["osc_atr_ratio"].expanding(min_periods=20).rank(pct=True),
        ),
    )
    atr_daily_pct = num("osc_atr_daily_percentile", "atr_daily_percentile")
    out["osc_atr_daily_percentile"] = atr_daily_pct.where(atr_daily_pct.notna(), out["osc_atr_percentile"])

    # VWAP.
    vwap_legacy = num("vwap_fut", "fut_vwap", "vwap")
    if not bool(vwap_legacy.notna().any()):
        typical = (high + low + close) / 3.0
        vol = num("fut_flow_volume", "fut_volume").fillna(0.0)
        vwap_legacy = (typical * vol).cumsum() / vol.cumsum().replace(0.0, np.nan)
    out["vwap_fut"] = vwap_legacy
    vwap_dist_legacy = num("vwap_distance")
    out["vwap_distance"] = vwap_dist_legacy.where(
        vwap_dist_legacy.notna(),
        (close - out["vwap_fut"]) / out["vwap_fut"].replace(0.0, np.nan),
    )

    # Distance and basis.
    day_high = high.cummax()
    day_low = low.cummin()
    dh_legacy = num("dist_from_day_high", "distance_from_day_high")
    dl_legacy = num("dist_from_day_low", "distance_from_day_low")
    out["dist_from_day_high"] = dh_legacy.where(
        dh_legacy.notna(),
        (close - day_high) / day_high.replace(0.0, np.nan),
    )
    out["dist_from_day_low"] = dl_legacy.where(
        dl_legacy.notna(),
        (close - day_low) / day_low.replace(0.0, np.nan),
    )
    basis = num("dist_basis", "basis")
    basis_fallback = close - out["px_spot_close"]
    out["dist_basis"] = basis.where(basis.notna(), basis_fallback)
    basis_chg = num("dist_basis_change_1m", "basis_change_1m")
    out["dist_basis_change_1m"] = basis_chg.where(basis_chg.notna(), out["dist_basis"].diff())

    # Futures flow.
    out["fut_flow_volume"] = num("fut_flow_volume", "fut_volume")
    out["fut_flow_oi"] = num("fut_flow_oi", "fut_oi")
    vol_roll = out["fut_flow_volume"].rolling(20, min_periods=5).mean()
    fut_rel_volume_20 = num("fut_flow_rel_volume_20", "fut_rel_volume_20")
    fut_rel_volume_fallback = out["fut_flow_volume"] / vol_roll.replace(0.0, np.nan)
    zero_fut_volume_windows = out["fut_flow_volume"].fillna(0.0).eq(0.0) & vol_roll.fillna(0.0).eq(0.0)
    fut_rel_volume_fallback.loc[zero_fut_volume_windows] = 0.0
    out["fut_flow_rel_volume_20"] = fut_rel_volume_20.where(
        fut_rel_volume_20.notna(),
        fut_rel_volume_fallback,
    )
    fut_volume_accel_1m = num("fut_flow_volume_accel_1m", "fut_volume_accel_1m")
    out["fut_flow_volume_accel_1m"] = fut_volume_accel_1m.where(
        fut_volume_accel_1m.notna(),
        out["fut_flow_volume"].pct_change(1, fill_method=None).replace([np.inf, -np.inf], np.nan),
    )
    fut_oi_change_1m = num("fut_flow_oi_change_1m", "fut_oi_change_1m")
    out["fut_flow_oi_change_1m"] = fut_oi_change_1m.where(
        fut_oi_change_1m.notna(),
        out["fut_flow_oi"].diff(1),
    )
    fut_oi_change_5m = num("fut_flow_oi_change_5m", "fut_oi_change_5m")
    out["fut_flow_oi_change_5m"] = fut_oi_change_5m.where(
        fut_oi_change_5m.notna(),
        out["fut_flow_oi"].diff(5),
    )
    oi_roll = out["fut_flow_oi"].rolling(20, min_periods=5).mean()
    oi_std_raw = out["fut_flow_oi"].rolling(20, min_periods=5).std(ddof=0)
    oi_std = oi_std_raw.replace(0.0, np.nan)
    fut_oi_rel_20 = num("fut_flow_oi_rel_20", "fut_oi_rel_20")
    fut_oi_rel_fallback = out["fut_flow_oi"] / oi_roll.replace(0.0, np.nan)
    zero_fut_oi_windows = out["fut_flow_oi"].fillna(0.0).eq(0.0) & oi_roll.fillna(0.0).eq(0.0)
    fut_oi_rel_fallback.loc[zero_fut_oi_windows] = 0.0
    out["fut_flow_oi_rel_20"] = fut_oi_rel_20.where(
        fut_oi_rel_20.notna(),
        fut_oi_rel_fallback,
    )
    fut_oi_zscore_20 = num("fut_flow_oi_zscore_20", "fut_oi_zscore_20")
    fut_oi_zscore_fallback = (out["fut_flow_oi"] - oi_roll) / oi_std
    zero_std_mask = oi_std_raw.fillna(0.0).eq(0.0) & (out["fut_flow_oi"] - oi_roll).fillna(0.0).eq(0.0)
    fut_oi_zscore_fallback.loc[zero_std_mask] = 0.0
    out["fut_flow_oi_zscore_20"] = fut_oi_zscore_20.where(
        fut_oi_zscore_20.notna(),
        fut_oi_zscore_fallback,
    )

    # Option flow.
    atm_strike = num("opt_flow_atm_strike", "atm_strike")
    atm_calc = (out["px_fut_close"] / 100.0).round() * 100.0
    out["opt_flow_atm_strike"] = atm_strike.where(atm_strike.notna(), atm_calc)
    opt_rows = num("opt_flow_rows", "options_rows", "strike_count")
    out["opt_flow_rows"] = opt_rows.fillna(0.0)
    ce_oi = num("opt_flow_ce_oi_total", "ce_oi_total", "total_ce_oi")
    pe_oi = num("opt_flow_pe_oi_total", "pe_oi_total", "total_pe_oi")
    out["opt_flow_ce_oi_total"] = ce_oi
    out["opt_flow_pe_oi_total"] = pe_oi
    out["opt_flow_ce_volume_total"] = num("opt_flow_ce_volume_total", "ce_volume_total")
    out["opt_flow_pe_volume_total"] = num("opt_flow_pe_volume_total", "pe_volume_total")
    no_option_rows = out["opt_flow_rows"].fillna(0.0) <= 0.0
    out.loc[no_option_rows, "opt_flow_ce_oi_total"] = out.loc[no_option_rows, "opt_flow_ce_oi_total"].fillna(0.0)
    out.loc[no_option_rows, "opt_flow_pe_oi_total"] = out.loc[no_option_rows, "opt_flow_pe_oi_total"].fillna(0.0)
    out.loc[no_option_rows, "opt_flow_ce_volume_total"] = out.loc[no_option_rows, "opt_flow_ce_volume_total"].fillna(0.0)
    out.loc[no_option_rows, "opt_flow_pe_volume_total"] = out.loc[no_option_rows, "opt_flow_pe_volume_total"].fillna(0.0)
    pcr = num("opt_flow_pcr_oi", "pcr_oi")
    pcr_fallback = num("pcr")
    pcr_calc = out["opt_flow_pe_oi_total"] / out["opt_flow_ce_oi_total"].replace(0.0, np.nan)
    out["opt_flow_pcr_oi"] = pcr.where(pcr.notna(), pcr_fallback.where(pcr_fallback.notna(), pcr_calc))
    out.loc[no_option_rows, "opt_flow_pcr_oi"] = out.loc[no_option_rows, "opt_flow_pcr_oi"].fillna(1.0)
    pcr_change_5m = num("pcr_change_5m")
    pcr_change_15m = num("pcr_change_15m")
    trade_date_groups = out["trade_date"].astype(str)
    pcr_diff_5m = out.groupby(trade_date_groups, sort=False)["opt_flow_pcr_oi"].diff(5)
    pcr_diff_15m = out.groupby(trade_date_groups, sort=False)["opt_flow_pcr_oi"].diff(15)
    out["pcr_change_5m"] = pcr_change_5m.where(pcr_change_5m.notna(), pcr_diff_5m)
    out["pcr_change_15m"] = pcr_change_15m.where(pcr_change_15m.notna(), pcr_diff_15m)

    call_ret_canonical = num("atm_ce_return_1m")
    put_ret_canonical = num("atm_pe_return_1m")
    call_ret_legacy = num("opt_flow_atm_call_return_1m", "atm_call_return_1m")
    put_ret_legacy = num("opt_flow_atm_put_return_1m", "atm_put_return_1m")
    atm_ce_close = num("atm_ce_close")
    atm_pe_close = num("atm_pe_close")
    atm_groups = out["trade_date"].astype(str)
    atm_ce_close_pct_change = atm_ce_close.groupby(atm_groups, sort=False).pct_change(1, fill_method=None)
    atm_pe_close_pct_change = atm_pe_close.groupby(atm_groups, sort=False).pct_change(1, fill_method=None)
    out["opt_flow_atm_call_return_1m"] = call_ret_canonical.where(
        call_ret_canonical.notna(),
        call_ret_legacy.where(
            call_ret_legacy.notna(),
            atm_ce_close_pct_change,
        ),
    )
    out["opt_flow_atm_put_return_1m"] = put_ret_canonical.where(
        put_ret_canonical.notna(),
        put_ret_legacy.where(
            put_ret_legacy.notna(),
            atm_pe_close_pct_change,
        ),
    )
    atm_oi_change_legacy = num("opt_flow_atm_oi_change_1m", "atm_oi_change_1m")
    atm_ce_oi_change_canonical = num("atm_ce_oi_change_1m")
    atm_pe_oi_change_canonical = num("atm_pe_oi_change_1m")
    atm_oi_change_canonical = pd.Series(np.nan, index=work.index, dtype=float)
    canonical_mask = atm_ce_oi_change_canonical.notna() & atm_pe_oi_change_canonical.notna()
    atm_oi_change_canonical.loc[canonical_mask] = (
        atm_ce_oi_change_canonical.loc[canonical_mask] + atm_pe_oi_change_canonical.loc[canonical_mask]
    )
    atm_total_oi = num("atm_ce_oi") + num("atm_pe_oi")
    atm_total_oi_diff = atm_total_oi.groupby(atm_groups, sort=False).diff()
    out["opt_flow_atm_oi_change_1m"] = atm_oi_change_canonical.where(
        atm_oi_change_canonical.notna(),
        atm_oi_change_legacy.where(
            atm_oi_change_legacy.notna(),
            atm_total_oi_diff,
        ),
    )
    atm_ce_oi_raw = num("atm_ce_oi")
    atm_pe_oi_raw = num("atm_pe_oi")
    atm_ratio = (atm_ce_oi_raw / (atm_ce_oi_raw + atm_pe_oi_raw).replace(0.0, np.nan)).where(
        atm_ce_oi_raw.notna() & atm_pe_oi_raw.notna()
    )
    out["atm_oi_ratio"] = num("atm_oi_ratio").where(num("atm_oi_ratio").notna(), atm_ratio)
    near_ratio = num("near_atm_oi_ratio")
    out["near_atm_oi_ratio"] = near_ratio.where(near_ratio.notna(), out["atm_oi_ratio"])
    ce_pe_oi_diff = num("opt_flow_ce_pe_oi_diff", "ce_pe_oi_diff")
    ce_pe_vol_diff = num("opt_flow_ce_pe_volume_diff", "ce_pe_volume_diff")
    options_total = num("opt_flow_options_volume_total", "options_volume_total")
    out["opt_flow_ce_pe_oi_diff"] = ce_pe_oi_diff.where(
        ce_pe_oi_diff.notna(),
        out["opt_flow_ce_oi_total"] - out["opt_flow_pe_oi_total"],
    )
    out["opt_flow_ce_pe_volume_diff"] = ce_pe_vol_diff.where(
        ce_pe_vol_diff.notna(),
        out["opt_flow_ce_volume_total"] - out["opt_flow_pe_volume_total"],
    )
    out["opt_flow_options_volume_total"] = options_total.where(
        options_total.notna(),
        out["opt_flow_ce_volume_total"] + out["opt_flow_pe_volume_total"],
    )
    opt_vol_roll = out["opt_flow_options_volume_total"].rolling(20, min_periods=5).mean()
    opt_rel_volume_20 = num("opt_flow_rel_volume_20", "options_rel_volume_20")
    opt_rel_volume_fallback = out["opt_flow_options_volume_total"] / opt_vol_roll.replace(0.0, np.nan)
    zero_opt_volume_windows = (
        out["opt_flow_options_volume_total"].fillna(0.0).eq(0.0)
        & opt_vol_roll.fillna(0.0).eq(0.0)
    )
    opt_rel_volume_fallback.loc[zero_opt_volume_windows] = 0.0
    out["opt_flow_rel_volume_20"] = opt_rel_volume_20.where(
        opt_rel_volume_20.notna(),
        opt_rel_volume_fallback,
    )

    # Time/session fields.
    minute_of_day = num("time_minute_of_day", "minute_of_day")
    minute_of_day_calc = (work["timestamp"].dt.hour * 60 + work["timestamp"].dt.minute).astype(float)
    out["time_minute_of_day"] = minute_of_day.where(minute_of_day.notna(), minute_of_day_calc)
    dow = num("time_day_of_week", "day_of_week")
    out["time_day_of_week"] = dow.where(dow.notna(), work["timestamp"].dt.dayofweek.astype(float))
    out["time_minute_index"] = work.groupby(out["trade_date"], sort=False).cumcount().astype(float)

    # Context/regime.
    ready_legacy = num("ctx_opening_range_ready", "opening_range_ready")
    ready_calc = (out["time_minute_index"] >= 15).astype(float)
    out["ctx_opening_range_ready"] = ready_legacy.where(ready_legacy.notna(), ready_calc)

    up_legacy = num("ctx_opening_range_breakout_up", "opening_range_breakout_up", "orh_broken")
    down_legacy = num("ctx_opening_range_breakout_down", "opening_range_breakout_down", "orl_broken")
    if not bool(up_legacy.notna().any()):
        up_legacy = num("orh_broken")
    if not bool(down_legacy.notna().any()):
        down_legacy = num("orl_broken")
    out["ctx_opening_range_breakout_up"] = up_legacy.where(up_legacy.notna(), 0.0)
    out["ctx_opening_range_breakout_down"] = down_legacy.where(down_legacy.notna(), 0.0)

    dte = num("ctx_dte_days", "dte_days", "days_to_expiry")
    out["ctx_dte_days"] = dte
    is_expiry = num("ctx_is_expiry_day", "is_expiry_day")
    out["ctx_is_expiry_day"] = is_expiry.where(is_expiry.notna(), (out["ctx_dte_days"] == 0).astype(float))
    near = num("ctx_is_near_expiry", "is_near_expiry")
    out["ctx_is_near_expiry"] = near.where(
        near.notna(),
        ((out["ctx_dte_days"] >= 0.0) & (out["ctx_dte_days"] <= 1.0)).astype(float),
    )
    high_vix = num("ctx_is_high_vix_day", "is_high_vix_day")
    vix_prev = num("vix_prev_close")
    out["ctx_is_high_vix_day"] = high_vix.where(high_vix.notna(), (vix_prev >= 20.0).astype(float))

    reg_atr_high = num("ctx_regime_atr_high", "regime_atr_high")
    reg_atr_low = num("ctx_regime_atr_low", "regime_atr_low")
    out["ctx_regime_atr_high"] = reg_atr_high.where(reg_atr_high.notna(), (out["osc_atr_daily_percentile"] >= 0.75).astype(float))
    out["ctx_regime_atr_low"] = reg_atr_low.where(reg_atr_low.notna(), (out["osc_atr_daily_percentile"] <= 0.25).astype(float))
    reg_up = num("ctx_regime_trend_up", "regime_trend_up")
    reg_dn = num("ctx_regime_trend_down", "regime_trend_down")
    out["ctx_regime_trend_up"] = reg_up.where(reg_up.notna(), (out["ema_9_21_spread"] >= 0.0).astype(float))
    out["ctx_regime_trend_down"] = reg_dn.where(reg_dn.notna(), (out["ema_9_21_spread"] < 0.0).astype(float))
    reg_exp = num("ctx_regime_expiry_near", "regime_expiry_near")
    out["ctx_regime_expiry_near"] = reg_exp.where(reg_exp.notna(), out["ctx_is_near_expiry"])

    # Ensure required columns exist and final projection order.
    for col in required_cols:
        if col not in out.columns:
            out[col] = np.nan
    out = out.loc[:, required_cols].copy()
    return out.to_dict("records")


def _validate_ml_flat_rows_or_raise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return validate_snapshot_ml_flat_rows(rows, raise_on_error=True)


def _snapshot_record(
    snapshot: dict[str, Any],
    *,
    build_source: str,
    build_run_id: str,
) -> dict[str, Any]:
    session_context = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    trade_date = str(snapshot.get("trade_date") or session_context.get("date") or "").strip()
    timestamp = str(snapshot.get("timestamp") or session_context.get("timestamp") or "").strip()
    return {
        "trade_date": trade_date,
        "timestamp": timestamp,
        "snapshot_id": str(snapshot.get("snapshot_id") or "").strip(),
        "schema_name": str(snapshot.get("schema_name") or "").strip(),
        "schema_version": str(snapshot.get("schema_version") or "").strip(),
        "instrument": str(snapshot.get("instrument") or "").strip(),
        "build_source": str(build_source),
        "build_run_id": str(build_run_id),
        "snapshot_raw_json": json.dumps(snapshot, ensure_ascii=False, default=str),
    }


def _flatten_snapshot(snapshot: dict[str, Any], trade_date: str, year: int) -> dict[str, Any]:
    """Flatten a MarketSnapshot into the intermediate source row used for v1 projection."""
    row: dict[str, Any] = {
        "trade_date": trade_date,
        "year": year,
        "instrument": snapshot.get("instrument"),
        "schema_version": snapshot.get("schema_version", FINAL_SNAPSHOT_SCHEMA_VERSION),
        "schema_name": snapshot.get("schema_name", "MarketSnapshot"),
        "snapshot_id": snapshot.get("snapshot_id"),
    }

    for block_name in (
        "session_context",
        "futures_bar",
        "futures_derived",
        "mtf_derived",
        "opening_range",
        "vix_context",
        "chain_aggregates",
        "ladder_aggregates",
        "atm_options",
        "iv_derived",
        "option_price",
        "session_levels",
    ):
        block = snapshot.get(block_name)
        if isinstance(block, dict):
            for key, value in block.items():
                row[key] = value

    return row


def process_day(
    *,
    trade_date: str,
    store: ParquetStore,
    vix_daily: pd.DataFrame,
    iv_carrier: IVStateCarrier,
    iv_diag: Optional[dict[str, int]] = None,
    instrument: str = "BANKNIFTY-I",
    lookback_days: int = LOOKBACK_DAYS,
    output_dataset: str = OUTPUT_DATASET_ML_FLAT,
    build_source: str = "historical",
    build_run_id: str | None = None,
    validate_ml_flat_contract: bool = False,
    emit_outputs: bool = True,
    futures_window_days: list[str] | None = None,
    preloaded_fut_window: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Build one day's minute-level snapshots from Layer-1 parquet inputs."""
    day_started_at = time.perf_counter()
    resolved_build_run_id = str(build_run_id or _default_build_run_id())
    if preloaded_fut_window is not None:
        fut_window = preloaded_fut_window.copy()
    elif futures_window_days:
        fut_window = store.futures_window_for_days(futures_window_days)
    else:
        fut_window = store.futures_window(trade_date, lookback_days=lookback_days)
    if len(fut_window) == 0:
        raise MissingInputsError(f"no futures rows available for day={trade_date}")

    options_day = store.options_for_day(trade_date)
    if len(options_day) == 0:
        raise MissingInputsError(f"no options rows available for day={trade_date}")
    spot_day = store.spot_for_day(trade_date)

    fut_window = fut_window.sort_values("timestamp").reset_index(drop=True)
    fut_window["trade_date"] = fut_window["trade_date"].astype(str)

    today_mask = fut_window["trade_date"] == trade_date
    if int(today_mask.sum()) == 0:
        raise MissingInputsError(f"no futures minute bars for day={trade_date}")

    chains_by_ts = _build_all_chains(options_day)
    spot_by_ts = _build_spot_map(
        spot_day,
        fut_timestamps=fut_window.loc[today_mask, "timestamp"],
    )
    atr_daily_percentile = _compute_daily_atr_percentile(fut_window, trade_date)

    state = MarketSnapshotState()
    iv_carrier.seed_state(state)
    prepared_window = prepare_market_snapshot_window(
        fut_window,
        current_trade_date=pd.Timestamp(trade_date),
    )

    source_rows: list[dict[str, Any]] = []
    snapshot_rows: list[dict[str, Any]] = []
    year = int(pd.Timestamp(trade_date).year)
    today_indices = fut_window.index[today_mask].tolist()

    day_iv_diag = iv_diag if isinstance(iv_diag, dict) else _new_iv_diag()

    total_minutes = len(today_indices)
    for idx_in_day, full_idx in enumerate(today_indices, start=1):
        bar = fut_window.iloc[full_idx]
        ts_key = _ts_key(bar["timestamp"])
        ts_min_key = _ts_key(pd.Timestamp(bar["timestamp"]).floor("min"))
        chain = chains_by_ts.get(ts_key) or chains_by_ts.get(ts_min_key, _EMPTY_CHAIN)

        snapshot = build_market_snapshot(
            instrument=instrument,
            ohlc=fut_window,
            chain=chain,
            state=state,
            vix_daily=vix_daily,
            vix_live_current=None,
            prepared_window=prepared_window,
            current_index=full_idx,
        )
        _update_iv_diag(day_iv_diag, snapshot, chain)
        if emit_outputs:
            validate_market_snapshot(snapshot, raise_on_error=True)
            snapshot_rows.append(
                _snapshot_record(
                    snapshot,
                    build_source=build_source,
                    build_run_id=resolved_build_run_id,
                )
            )
            row = _flatten_snapshot(snapshot, trade_date=trade_date, year=year)
            row["build_source"] = str(build_source)
            row["build_run_id"] = resolved_build_run_id
            spot_values = spot_by_ts.get(ts_key) or spot_by_ts.get(ts_min_key, {})
            row["spot_open"] = pd.to_numeric(spot_values.get("spot_open"), errors="coerce")
            row["spot_high"] = pd.to_numeric(spot_values.get("spot_high"), errors="coerce")
            row["spot_low"] = pd.to_numeric(spot_values.get("spot_low"), errors="coerce")
            row["spot_close"] = pd.to_numeric(spot_values.get("spot_close"), errors="coerce")
            row["atr_daily_percentile"] = atr_daily_percentile

            ce_volume_total, pe_volume_total, options_rows = _chain_totals(chain)
            row["ce_volume_total"] = ce_volume_total
            row["pe_volume_total"] = pe_volume_total
            row["options_rows"] = options_rows
            row["options_volume_total"] = (
                ce_volume_total + pe_volume_total
                if np.isfinite(ce_volume_total) and np.isfinite(pe_volume_total)
                else float("nan")
            )
            source_rows.append(row)
        if (
            idx_in_day == 1
            or idx_in_day == total_minutes
            or (idx_in_day % DAY_PROGRESS_EVERY_MINUTES) == 0
        ):
            elapsed = time.perf_counter() - day_started_at
            print(
                f"[snapshot_batch]   day={trade_date} minute={idx_in_day}/{total_minutes} "
                f"elapsed_sec={elapsed:.1f}",
                flush=True,
            )

    iv_carrier.absorb_state(state)
    if not emit_outputs:
        return {
            "snapshot_rows": [],
            "market_base_rows": [],
        }
    return {
        "snapshot_rows": snapshot_rows,
        "market_base_rows": source_rows,
    }


def _write_parquet_atomic(frame: pd.DataFrame, out_path: Path) -> None:
    """Write parquet via a same-directory temp file so failed writes leave the prior file intact."""
    temp_path = out_path.with_name(
        f"{out_path.stem}.tmp_{os.getpid()}_{uuid.uuid4().hex}{out_path.suffix}"
    )
    try:
        frame.to_parquet(temp_path, index=False, compression="snappy")
        temp_path.replace(out_path)
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            logger.warning("failed to remove temp parquet after write error path=%s", temp_path, exc_info=True)
        raise


def write_days_to_parquet(
    rows: list[dict[str, Any]],
    *,
    out_base: Path,
    year: int,
    output_dataset: str = OUTPUT_DATASET_ML_FLAT,
    replace_trade_dates: set[str] | None = None,
    partition_key: str | None = None,
) -> int:
    """Idempotently write one or more days into one snapshot parquet partition."""
    if not rows:
        return 0

    out_dir = _chunk_out_dir(
        out_base=out_base,
        output_dataset=output_dataset,
        year=year,
        partition_key=partition_key,
    )
    out_path = out_dir / "data.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)

    new_df = pd.DataFrame(rows)
    trade_dates = {str(x) for x in new_df["trade_date"].astype(str).tolist()}
    if replace_trade_dates:
        trade_dates.update({str(x) for x in replace_trade_dates})

    if out_path.exists():
        try:
            existing = pd.read_parquet(out_path)
        except Exception as exc:
            # Interrupted writes can leave a truncated yearly parquet file.
            # Quarantine the corrupt file so the current run can rebuild safely.
            stamp = pd.Timestamp.utcnow().strftime("%Y%m%d_%H%M%S")
            corrupt_path = out_dir / f"data.corrupt_{stamp}.parquet"
            try:
                out_path.replace(corrupt_path)
                logger.warning(
                    "detected corrupt snapshot parquet year=%s path=%s moved_to=%s err=%s",
                    year,
                    out_path,
                    corrupt_path,
                    exc,
                )
            except Exception:
                logger.exception(
                    "failed to quarantine corrupt snapshot parquet year=%s path=%s err=%s",
                    year,
                    out_path,
                    exc,
                )
            existing = pd.DataFrame()
        if "trade_date" in existing.columns and trade_dates:
            existing = existing[~existing["trade_date"].astype(str).isin(trade_dates)]
        if len(existing):
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
    else:
        combined = new_df

    combined["_sort_timestamp"] = pd.to_datetime(combined.get("timestamp"), errors="coerce")
    sort_cols = ["_sort_timestamp"]
    if "snapshot_id" in combined.columns:
        sort_cols.append("snapshot_id")
    combined = combined.sort_values(sort_cols).drop(columns=["_sort_timestamp"]).reset_index(drop=True)
    _write_parquet_atomic(combined, out_path)
    return len(new_df)


def write_day_to_parquet(
    rows: list[dict[str, Any]],
    out_base: Path,
    year: int,
    *,
    output_dataset: str = OUTPUT_DATASET_ML_FLAT,
    partition_key: str | None = None,
) -> int:
    """Compatibility wrapper for one-day writes."""
    trade_date = None
    if rows:
        first = rows[0]
        if isinstance(first, dict):
            trade_date = str(first.get("trade_date") or "").strip() or None
    replace_dates = {trade_date} if trade_date else None
    return write_days_to_parquet(
        rows,
        out_base=out_base,
        year=year,
        output_dataset=output_dataset,
        replace_trade_dates=replace_dates,
        partition_key=partition_key,
    )


def _completed_output_days(
    *,
    parquet_base: str | Path,
    min_day: str | None,
    max_day: str | None,
    requested_days: set[str] | None,
    dataset_names: tuple[str, ...] | list[str] | None = None,
) -> set[str]:
    day_sets: list[set[str]] = []
    for dataset_name in (tuple(dataset_names) if dataset_names else _all_output_datasets()):
        store = ParquetStore(parquet_base, snapshots_dataset=dataset_name)
        days = set(store.available_snapshot_days(min_day=min_day, max_day=max_day))
        if requested_days:
            days = days.intersection(requested_days)
        day_sets.append(days)
    if not day_sets:
        return set()
    completed = set.intersection(*day_sets)
    if requested_days:
        completed = completed.intersection(requested_days)
    return completed


def run_snapshot_batch(
    *,
    parquet_base: str | Path,
    instrument: str = "BANKNIFTY-I",
    min_day: str | None = None,
    max_day: str | None = None,
    explicit_days: list[str] | None = None,
    planned_days: list[str] | None = None,
    emit_days: list[str] | None = None,
    initial_state: IVStateCarrier | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    resume: bool = True,
    dry_run: bool = False,
    log_every: int = 10,
    write_batch_days: int = 20,
    output_dataset: str = OUTPUT_DATASET_ML_FLAT,
    build_source: str = "historical",
    build_run_id: str | None = None,
    validate_ml_flat_contract: bool = False,
    partition_key: str | None = None,
) -> dict[str, Any]:
    """Build historical Layer-2 snapshots from Layer-1 parquet data."""
    started_at = time.time()
    resolved_build_run_id = str(build_run_id or _default_build_run_id())
    store = ParquetStore(parquet_base, snapshots_dataset=OUTPUT_DATASET_SNAPSHOTS)
    out_base = Path(parquet_base)

    requested_days = {str(day) for day in (emit_days or explicit_days or []) if str(day).strip()}
    planned_day_values = [str(day) for day in (planned_days or []) if str(day).strip()]
    min_bound = min_day
    max_bound = max_day
    if planned_day_values:
        min_bound = min(planned_day_values)
        max_bound = max(planned_day_values)
    elif requested_days:
        min_bound = min_bound or min(requested_days)
        max_bound = max_bound or max(requested_days)

    history_calendar_days = store.available_days(min_day=None, max_day=max_bound)
    available_calendar_days = [
        day
        for day in history_calendar_days
        if (min_bound is None or str(day) >= str(min_bound))
        and (max_bound is None or str(day) <= str(max_bound))
    ]
    execution_days = list(available_calendar_days)
    if planned_day_values:
        planned_day_set = set(planned_day_values)
        execution_days = [day for day in available_calendar_days if day in planned_day_set]

    output_days = list(execution_days)
    if requested_days:
        output_days = [day for day in execution_days if day in requested_days]
        if not output_days and planned_day_values:
            output_days = [day for day in available_calendar_days if day in requested_days]

    if not output_days:
        return {
            "status": "no_days",
            "output_dataset": OUTPUT_DATASET_SNAPSHOTS,
            **_canonical_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": 0,
        }

    options_days_with_data = set(store.all_days_with_options(min_day=min_bound, max_day=max_bound))
    history_index = {str(day): idx for idx, day in enumerate(history_calendar_days)}
    futures_window_days_by_day: dict[str, list[str]] = {}
    for day in execution_days:
        idx = history_index.get(str(day))
        if idx is None:
            continue
        start_idx = max(0, int(idx) - max(0, int(lookback_days)))
        futures_window_days_by_day[str(day)] = [str(value) for value in history_calendar_days[start_idx : idx + 1]]
    futures_window_cache = _preload_futures_windows(
        store=store,
        history_calendar_days=history_calendar_days,
        execution_days=execution_days,
        futures_window_days_by_day=futures_window_days_by_day,
    )

    already_done = (
        _completed_output_days(
            parquet_base=parquet_base,
            min_day=min_bound,
            max_day=max_bound,
            requested_days=set(output_days),
            dataset_names=CANONICAL_OUTPUT_DATASETS,
        )
        if resume
        else set()
    )
    pending_output_days = [day for day in output_days if day not in already_done]
    pending_output_set = set(pending_output_days)

    print(f"[snapshot_batch] Days available        : {len(output_days)}")
    print(f"[snapshot_batch] Days already built    : {len(already_done)}")
    print(f"[snapshot_batch] Days pending          : {len(pending_output_days)}")
    print(
        "[snapshot_batch] Output datasets       : "
        + ", ".join(CANONICAL_OUTPUT_DATASETS)
    )
    print(f"[snapshot_batch] Build source/run      : {build_source}/{resolved_build_run_id}")
    if min_day or max_day:
        print(f"[snapshot_batch] Date filter           : {min_day or 'start'} -> {max_day or 'end'}")
    if requested_days:
        print(f"[snapshot_batch] Explicit day mode     : {len(output_days)} requested")
    warmup_days = max(0, int(len(execution_days) - len(output_days)))
    if warmup_days > 0:
        print(f"[snapshot_batch] Warmup execution days : {warmup_days}")
    if partition_key:
        print(f"[snapshot_batch] Output partition      : {partition_key}")

    skipped_missing_inputs: list[str] = []
    dry_ready: list[str] = []

    if dry_run:
        for day in pending_output_days:
            if day in options_days_with_data:
                dry_ready.append(day)
            else:
                skipped_missing_inputs.append(day)

        return {
            "status": "dry_run",
            "output_dataset": OUTPUT_DATASET_SNAPSHOTS,
            **_canonical_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": len(output_days),
            "days_pending": len(pending_output_days),
            "days_ready": len(dry_ready),
            "days_skipped_existing": len(already_done),
            "days_skipped_missing_inputs": len(skipped_missing_inputs),
            "missing_input_days": skipped_missing_inputs[:50],
            "first_ready_day": dry_ready[0] if dry_ready else None,
            "last_ready_day": dry_ready[-1] if dry_ready else None,
        }

    if not pending_output_days:
        return {
            "status": "already_complete",
            "output_dataset": OUTPUT_DATASET_SNAPSHOTS,
            **_canonical_contract_validation_metadata(validate_ml_flat_contract),
            "days_available": len(output_days),
            "days_skipped_existing": len(already_done),
            "days_pending": 0,
        }

    vix_daily = store.vix()
    iv_carrier = initial_state.clone() if isinstance(initial_state, IVStateCarrier) else IVStateCarrier()
    days_done = 0
    warmup_days_processed = 0
    total_rows = 0
    total_market_base_rows = 0
    iv_diag_total = _new_iv_diag()
    iv_diag_by_day: list[dict[str, Any]] = []
    no_rows_days: list[str] = []
    errors: list[dict[str, str]] = []
    batch_days = max(1, int(write_batch_days))
    buffered_year: int | None = None
    buffered_snapshot_rows: list[dict[str, Any]] = []
    buffered_market_base_rows: list[dict[str, Any]] = []
    buffered_trade_dates: set[str] = set()
    buffered_day_count = 0
    total_snapshot_rows = 0

    def _flush_buffer() -> None:
        nonlocal total_rows, total_market_base_rows, total_snapshot_rows, buffered_snapshot_rows, buffered_market_base_rows, buffered_trade_dates, buffered_day_count
        if buffered_year is None or (not buffered_snapshot_rows and not buffered_market_base_rows):
            return
        snapshot_written = write_days_to_parquet(
            buffered_snapshot_rows,
            out_base=out_base,
            year=buffered_year,
            output_dataset=OUTPUT_DATASET_SNAPSHOTS,
            replace_trade_dates=buffered_trade_dates,
            partition_key=partition_key,
        )
        market_base_written = write_days_to_parquet(
            buffered_market_base_rows,
            out_base=out_base,
            year=buffered_year,
            output_dataset=OUTPUT_DATASET_MARKET_BASE,
            replace_trade_dates=buffered_trade_dates,
            partition_key=partition_key,
        )
        total_snapshot_rows += snapshot_written
        total_market_base_rows += market_base_written
        total_rows += market_base_written
        buffered_snapshot_rows = []
        buffered_market_base_rows = []
        buffered_trade_dates = set()
        buffered_day_count = 0

    for idx, day in enumerate(execution_days):
        if idx == 0 or (idx % max(1, int(log_every)) == 0):
            print(f"[snapshot_batch] Processing {idx + 1}/{len(execution_days)} day={day}", flush=True)

        if day not in options_days_with_data:
            if day in pending_output_set:
                skipped_missing_inputs.append(day)
            continue

        try:
            day_started_at = time.perf_counter()
            day_iv_diag = _new_iv_diag()
            emit_output_day = day in pending_output_set
            rows = process_day(
                trade_date=day,
                store=store,
                vix_daily=vix_daily,
                iv_carrier=iv_carrier,
                iv_diag=day_iv_diag,
                instrument=instrument,
                lookback_days=lookback_days,
                output_dataset=output_dataset,
                build_source=build_source,
                build_run_id=resolved_build_run_id,
                validate_ml_flat_contract=False,
                emit_outputs=emit_output_day,
                futures_window_days=futures_window_days_by_day.get(str(day)),
                preloaded_fut_window=futures_window_cache.get(str(day)),
            )
            if not emit_output_day:
                warmup_days_processed += 1
                continue
            snapshot_rows = list(rows.get("snapshot_rows") or [])
            market_base_rows = list(rows.get("market_base_rows") or [])
            if not snapshot_rows or not market_base_rows:
                no_rows_days.append(day)
                continue

            _merge_iv_diag(iv_diag_total, day_iv_diag)
            if (
                int(day_iv_diag.get("ce_iv_solver_failed", 0)) > 0
                or int(day_iv_diag.get("pe_iv_solver_failed", 0)) > 0
                or int(day_iv_diag.get("ce_iv_unexpected_missing", 0)) > 0
                or int(day_iv_diag.get("pe_iv_unexpected_missing", 0)) > 0
            ):
                iv_diag_by_day.append({"trade_date": str(day), **day_iv_diag})

            year = int(pd.Timestamp(day).year)
            if buffered_year is None:
                buffered_year = year
            if year != buffered_year:
                _flush_buffer()
                buffered_year = year

            buffered_snapshot_rows.extend(snapshot_rows)
            buffered_market_base_rows.extend(market_base_rows)
            buffered_trade_dates.add(str(day))
            buffered_day_count += 1
            if buffered_day_count >= batch_days:
                _flush_buffer()
            days_done += 1
            day_elapsed = time.perf_counter() - day_started_at
            print(
                f"[snapshot_batch] Completed day={day} snapshots={len(snapshot_rows)} market_base_rows={len(market_base_rows)} elapsed_sec={day_elapsed:.1f}",
                flush=True,
            )
        except MissingInputsError:
            if day in pending_output_set:
                skipped_missing_inputs.append(day)
            else:
                raise
        except Exception as exc:
            logger.exception("snapshot batch failed day=%s err=%s", day, exc)
            if day in pending_output_set:
                errors.append({"day": day, "error": str(exc)})
            else:
                raise

    _flush_buffer()

    elapsed = round(time.time() - started_at, 2)
    final_status = "complete"
    if errors:
        final_status = "partial_error"
    elif skipped_missing_inputs or no_rows_days:
        final_status = "partial_incomplete"
    return {
        "status": final_status,
        "output_dataset": OUTPUT_DATASET_SNAPSHOTS,
        "build_source": build_source,
        "build_run_id": resolved_build_run_id,
        "partition_key": str(partition_key or ""),
        **_canonical_contract_validation_metadata(validate_ml_flat_contract),
        "days_available": len(output_days),
        "days_pending": len(pending_output_days),
        "days_processed": days_done,
        "warmup_days_processed": int(warmup_days_processed),
        "days_skipped_existing": len(already_done),
        "days_skipped_missing_inputs": len(skipped_missing_inputs),
        "missing_input_days": skipped_missing_inputs[:50],
        "days_no_rows": len(no_rows_days),
        "no_row_days": no_rows_days[:50],
        "error_count": len(errors),
        "error_days": [entry["day"] for entry in errors],
        "total_rows": int(total_rows),
        "total_snapshot_rows": int(total_snapshot_rows),
        "total_market_base_rows": int(total_market_base_rows),
        "written_datasets": list(CANONICAL_OUTPUT_DATASETS),
        "iv_diagnostics": iv_diag_total,
        "iv_diagnostics_days_with_failures": iv_diag_by_day,
        "elapsed_sec": elapsed,
    }
