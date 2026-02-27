"""Historical Layer-2 snapshot builder."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

import pandas as pd

from snapshot_app.market_snapshot import MarketSnapshotState, build_market_snapshot

from .parquet_store import ParquetStore

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 30
IV_HISTORY_MAXLEN = 30_000
CHAIN_HISTORY_MAXLEN = 4_000


class MissingInputsError(RuntimeError):
    """Raised when a day cannot be processed due to incomplete upstream parquet."""


@dataclass
class IVStateCarrier:
    """Carry state across days so derived metrics remain realistic."""

    iv_history_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=IV_HISTORY_MAXLEN))
    iv_history_non_expiry: Deque[float] = field(default_factory=lambda: deque(maxlen=IV_HISTORY_MAXLEN))
    chain_history: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=CHAIN_HISTORY_MAXLEN))

    def seed_state(self, state: MarketSnapshotState) -> None:
        for value in self.iv_history_expiry:
            state.iv_history_expiry.append(value)
        for value in self.iv_history_non_expiry:
            state.iv_history_non_expiry.append(value)
        for item in self.chain_history:
            state.chain_history.append(item)

    def absorb_state(self, state: MarketSnapshotState) -> None:
        for value in state.iv_history_expiry:
            self.iv_history_expiry.append(value)
        for value in state.iv_history_non_expiry:
            self.iv_history_non_expiry.append(value)
        for item in state.chain_history:
            self.chain_history.append(item)


def _ts_key(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _chain_from_options_minute(options_minute: pd.DataFrame) -> dict[str, Any]:
    """Build snapshot_app-compatible chain dict from one minute options rows."""
    if options_minute is None or len(options_minute) == 0:
        return {"expiry": None, "pcr": None, "max_pain": None, "strikes": []}

    work = options_minute.copy()
    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["close"] = pd.to_numeric(work.get("close"), errors="coerce")
    work["oi"] = pd.to_numeric(work.get("oi"), errors="coerce")
    work["volume"] = pd.to_numeric(work.get("volume"), errors="coerce")
    work["option_type"] = work.get("option_type", "").astype(str).str.upper().str.strip()
    work = work.dropna(subset=["strike"])

    expiry = None
    if "expiry_str" in work.columns:
        non_null = work["expiry_str"].dropna()
        if len(non_null):
            expiry = str(non_null.iloc[0]).strip().upper()

    def _last_float(sub: pd.DataFrame, col: str) -> Optional[float]:
        if col not in sub.columns or len(sub) == 0:
            return None
        value = pd.to_numeric(sub[col].iloc[-1], errors="coerce")
        if pd.isna(value):
            return None
        return float(value)

    strikes: list[dict[str, Any]] = []
    total_ce_oi = 0.0
    total_pe_oi = 0.0

    for strike_value, group in work.groupby("strike", sort=True):
        ce = group[group["option_type"] == "CE"]
        pe = group[group["option_type"] == "PE"]

        ce_oi = float(pd.to_numeric(ce.get("oi"), errors="coerce").fillna(0.0).sum()) if len(ce) else 0.0
        pe_oi = float(pd.to_numeric(pe.get("oi"), errors="coerce").fillna(0.0).sum()) if len(pe) else 0.0
        ce_volume = float(pd.to_numeric(ce.get("volume"), errors="coerce").fillna(0.0).sum()) if len(ce) else 0.0
        pe_volume = float(pd.to_numeric(pe.get("volume"), errors="coerce").fillna(0.0).sum()) if len(pe) else 0.0

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        strikes.append(
            {
                "strike": float(strike_value),
                "ce_ltp": _last_float(ce, "close"),
                "pe_ltp": _last_float(pe, "close"),
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
                "CE": {"last_price": _last_float(ce, "close"), "oi": ce_oi, "volume": ce_volume},
                "PE": {"last_price": _last_float(pe, "close"), "oi": pe_oi, "volume": pe_volume},
            }
        )

    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None
    return {"expiry": expiry, "pcr": pcr, "max_pain": None, "strikes": strikes}


def _flatten_snapshot(snapshot: dict[str, Any], trade_date: str, year: int) -> dict[str, Any]:
    """Flatten MSS blocks into one row suitable for analytics and replay."""
    row: dict[str, Any] = {
        "trade_date": trade_date,
        "year": year,
        "instrument": snapshot.get("instrument"),
        "schema_version": snapshot.get("version", "1.0"),
        "schema_name": snapshot.get("schema_name", "MarketSnapshot"),
        "snapshot_id": snapshot.get("snapshot_id"),
    }

    for block_name in (
        "session_context",
        "futures_bar",
        "futures_derived",
        "opening_range",
        "vix_context",
        "chain_aggregates",
        "atm_options",
        "iv_derived",
        "session_levels",
    ):
        block = snapshot.get(block_name)
        if isinstance(block, dict):
            for key, value in block.items():
                row[key] = value

    row["snapshot_raw_json"] = json.dumps(snapshot, ensure_ascii=False, default=str)
    return row


def process_day(
    *,
    trade_date: str,
    store: ParquetStore,
    vix_daily: pd.DataFrame,
    iv_carrier: IVStateCarrier,
    instrument: str = "BANKNIFTY-I",
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Build one day's minute-level snapshots from Layer-1 parquet inputs."""
    fut_window = store.futures_window(trade_date, lookback_days=lookback_days)
    if len(fut_window) == 0:
        raise MissingInputsError(f"no futures rows available for day={trade_date}")

    options_day = store.options_for_day(trade_date)
    if len(options_day) == 0:
        raise MissingInputsError(f"no options rows available for day={trade_date}")

    fut_window = fut_window.sort_values("timestamp").reset_index(drop=True)
    fut_window["trade_date"] = fut_window["trade_date"].astype(str)

    today_mask = fut_window["trade_date"] == trade_date
    if int(today_mask.sum()) == 0:
        raise MissingInputsError(f"no futures minute bars for day={trade_date}")

    options_by_ts: dict[str, pd.DataFrame] = {}
    for ts, group in options_day.groupby(options_day["timestamp"].map(_ts_key), sort=False):
        if ts:
            options_by_ts[str(ts)] = group

    state = MarketSnapshotState()
    iv_carrier.seed_state(state)

    rows: list[dict[str, Any]] = []
    year = int(pd.Timestamp(trade_date).year)
    today_indices = fut_window.index[today_mask].tolist()

    for full_idx in today_indices:
        bar = fut_window.iloc[full_idx]
        ts_key = _ts_key(bar["timestamp"])
        minute_options = options_by_ts.get(ts_key, pd.DataFrame())
        chain = _chain_from_options_minute(minute_options)

        bars_up_to_now = fut_window.iloc[: full_idx + 1]
        snapshot = build_market_snapshot(
            instrument=instrument,
            ohlc=bars_up_to_now,
            chain=chain,
            state=state,
            vix_daily=vix_daily,
            vix_live_current=None,
        )
        rows.append(_flatten_snapshot(snapshot, trade_date=trade_date, year=year))

    iv_carrier.absorb_state(state)
    return rows


def write_day_to_parquet(rows: list[dict[str, Any]], out_base: Path, year: int) -> int:
    """Idempotently write one day into yearly snapshots parquet."""
    if not rows:
        return 0

    out_dir = out_base / "snapshots" / f"year={year}"
    out_path = out_dir / "data.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)

    new_df = pd.DataFrame(rows)
    trade_date = str(new_df["trade_date"].iloc[0])

    if out_path.exists():
        existing = pd.read_parquet(out_path)
        if "trade_date" in existing.columns:
            existing = existing[existing["trade_date"].astype(str) != trade_date]
        if len(existing):
            merged_records = existing.to_dict("records") + new_df.to_dict("records")
            combined = pd.DataFrame.from_records(merged_records)
        else:
            combined = new_df
    else:
        combined = new_df

    combined["_sort_timestamp"] = pd.to_datetime(combined.get("timestamp"), errors="coerce")
    sort_cols = ["_sort_timestamp"]
    if "snapshot_id" in combined.columns:
        sort_cols.append("snapshot_id")
    combined = combined.sort_values(sort_cols).drop(columns=["_sort_timestamp"]).reset_index(drop=True)
    combined.to_parquet(out_path, index=False, compression="snappy")
    return len(new_df)


def run_snapshot_batch(
    *,
    parquet_base: str | Path,
    instrument: str = "BANKNIFTY-I",
    min_day: str | None = None,
    max_day: str | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    resume: bool = True,
    dry_run: bool = False,
    log_every: int = 10,
) -> dict[str, Any]:
    """Build historical Layer-2 snapshots from Layer-1 parquet data."""
    started_at = time.time()
    store = ParquetStore(parquet_base)
    out_base = Path(parquet_base)

    all_days = store.available_days(min_day=min_day, max_day=max_day)
    if not all_days:
        return {"status": "no_days", "days_available": 0}

    already_done = set(store.available_snapshot_days(min_day=min_day, max_day=max_day)) if resume else set()
    pending_days = [day for day in all_days if day not in already_done]

    print(f"[snapshot_batch] Days available        : {len(all_days)}")
    print(f"[snapshot_batch] Days already built    : {len(already_done)}")
    print(f"[snapshot_batch] Days pending          : {len(pending_days)}")
    if min_day or max_day:
        print(f"[snapshot_batch] Date filter           : {min_day or 'start'} -> {max_day or 'end'}")

    skipped_missing_inputs: list[str] = []
    dry_ready: list[str] = []

    if dry_run:
        for day in pending_days:
            if store.has_options_for_day(day):
                dry_ready.append(day)
            else:
                skipped_missing_inputs.append(day)

        return {
            "status": "dry_run",
            "days_available": len(all_days),
            "days_pending": len(pending_days),
            "days_ready": len(dry_ready),
            "days_skipped_existing": len(already_done),
            "days_skipped_missing_inputs": len(skipped_missing_inputs),
            "missing_input_days": skipped_missing_inputs[:50],
            "first_ready_day": dry_ready[0] if dry_ready else None,
            "last_ready_day": dry_ready[-1] if dry_ready else None,
        }

    if not pending_days:
        return {
            "status": "already_complete",
            "days_available": len(all_days),
            "days_skipped_existing": len(already_done),
            "days_pending": 0,
        }

    vix_daily = store.vix()
    iv_carrier = IVStateCarrier()
    days_done = 0
    total_rows = 0
    no_rows_days: list[str] = []
    errors: list[dict[str, str]] = []

    for idx, day in enumerate(pending_days):
        if idx == 0 or (idx % max(1, int(log_every)) == 0):
            print(f"[snapshot_batch] Processing {idx + 1}/{len(pending_days)} day={day}", flush=True)

        if not store.has_options_for_day(day):
            skipped_missing_inputs.append(day)
            continue

        try:
            rows = process_day(
                trade_date=day,
                store=store,
                vix_daily=vix_daily,
                iv_carrier=iv_carrier,
                instrument=instrument,
                lookback_days=lookback_days,
            )
            if not rows:
                no_rows_days.append(day)
                continue

            year = int(pd.Timestamp(day).year)
            written = write_day_to_parquet(rows, out_base=out_base, year=year)
            total_rows += written
            days_done += 1
        except MissingInputsError:
            skipped_missing_inputs.append(day)
        except Exception as exc:
            logger.exception("snapshot batch failed day=%s err=%s", day, exc)
            errors.append({"day": day, "error": str(exc)})

    elapsed = round(time.time() - started_at, 2)
    return {
        "status": "complete",
        "days_available": len(all_days),
        "days_pending": len(pending_days),
        "days_processed": days_done,
        "days_skipped_existing": len(already_done),
        "days_skipped_missing_inputs": len(skipped_missing_inputs),
        "days_no_rows": len(no_rows_days),
        "error_count": len(errors),
        "error_days": [entry["day"] for entry in errors],
        "total_rows": int(total_rows),
        "elapsed_sec": elapsed,
    }
