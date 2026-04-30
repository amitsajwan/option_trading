"""
Live velocity feature accumulator.

Accumulates minute-level live snapshots from 10:00–11:30 IST, computes
velocity features at the 11:30 tick, then injects them into every
snapshot dict for the remainder of that trade_date.

Usage (inside LiveMarketSnapshotBuilder.build_snapshot):
    snapshot = self._velocity_acc.process(snapshot)
    # snapshot["velocity_enrichment"] is populated from 11:30 onward
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from snapshot_app.core.velocity_features import compute_velocity_features

_log = logging.getLogger(__name__)

# Morning window: accumulate rows with timestamp in [10:00, 11:30)
_WINDOW_START: Tuple[int, int] = (10, 0)
_MIDDAY: Tuple[int, int] = (11, 30)

# Mapping: (snapshot_section, key_in_section) → morning_df column name
# Column names must match those consumed by compute_velocity_features().
_COLUMN_MAP: List[Tuple[str, str, str]] = [
    ("futures_bar",      "fut_open",         "px_fut_open"),
    ("futures_bar",      "fut_high",         "px_fut_high"),
    ("futures_bar",      "fut_low",          "px_fut_low"),
    ("futures_bar",      "fut_close",        "px_fut_close"),
    ("futures_derived",  "vwap",             "vwap_fut"),
    ("chain_aggregates", "total_ce_oi",      "opt_flow_ce_oi_total"),
    ("chain_aggregates", "total_pe_oi",      "opt_flow_pe_oi_total"),
    ("chain_aggregates", "total_ce_volume",  "opt_flow_ce_volume_total"),
    ("chain_aggregates", "total_pe_volume",  "opt_flow_pe_volume_total"),
    ("chain_aggregates", "pcr",              "opt_flow_pcr_oi"),
    ("chain_aggregates", "pcr_change_15m",   "pcr_change_15m"),
    ("atm_options",      "atm_oi_ratio",     "atm_oi_ratio"),
    ("atm_options",      "atm_ce_iv",        "atm_ce_iv"),
    ("atm_options",      "atm_pe_iv",        "atm_pe_iv"),
    ("iv_derived",       "iv_skew",          "iv_skew"),
]

# Columns read from historical flat parquet for context lookup.
_CONTEXT_READ_COLS: List[str] = [
    "timestamp",
    "px_fut_close",
    "opt_flow_ce_volume_total",
    "opt_flow_pe_volume_total",
]

DEFAULT_SOURCE_FLAT_DATASET = "snapshots_ml_flat_v2"


def _extract_morning_row(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one live snapshot into the column shape expected by compute_velocity_features."""
    row: Dict[str, Any] = {
        "timestamp":  snapshot.get("timestamp"),
        "trade_date": snapshot.get("trade_date"),
    }
    for section, src_key, dst_col in _COLUMN_MAP:
        sec = snapshot.get(section)
        row[dst_col] = sec.get(src_key) if isinstance(sec, dict) else None
    return row


def _hm(iso_ts: str) -> Tuple[int, int]:
    """Return (hour, minute) from an ISO timestamp; returns (-1, -1) on any error."""
    try:
        t = pd.Timestamp(iso_ts)
        return (t.hour, t.minute)
    except Exception:
        return (-1, -1)


def _load_context_for_date(
    dataset_root: Path,
    trade_date: str,
    *,
    lookback: int = 25,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Read prev_day context values from the flat-snapshot parquet.

    Returns:
        (prev_day_close, prev_day_midday_option_volume, avg_20d_midday_option_volume)
        Any element is None when data is unavailable.

    Reads the `lookback` most-recent parquet files whose stem (trade_date) is
    strictly before `trade_date`, sorted descending.  Per file it extracts:
      - last-of-day px_fut_close  → prev_day_close (index 0 = most recent day)
      - 11:30 total options volume → 20-day rolling average
    """
    if not dataset_root.exists():
        return None, None, None

    # Collect all per-day parquet files whose date precedes trade_date.
    all_files: List[Tuple[str, Path]] = []
    for year_dir in dataset_root.iterdir():
        if not year_dir.is_dir():
            continue
        for pq_file in year_dir.glob("*.parquet"):
            stem = pq_file.stem  # YYYY-MM-DD
            if stem < trade_date:
                all_files.append((stem, pq_file))

    all_files.sort(key=lambda x: x[0], reverse=True)
    recent = all_files[:lookback]

    if not recent:
        return None, None, None

    day_records: List[Dict[str, Any]] = []
    for date_str, pq_path in recent:
        try:
            df = pd.read_parquet(pq_path, columns=_CONTEXT_READ_COLS)
        except Exception:
            try:
                df = pd.read_parquet(pq_path)
                available = [c for c in _CONTEXT_READ_COLS if c in df.columns]
                df = df[available]
            except Exception:
                continue

        if df.empty or "timestamp" not in df.columns:
            continue

        df = df.copy()
        df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["_ts"]).sort_values("_ts")
        if df.empty:
            continue

        # Last-of-day futures close.
        last_close: Optional[float] = None
        if "px_fut_close" in df.columns:
            raw = pd.to_numeric(df["px_fut_close"].iloc[-1], errors="coerce")
            last_close = float(raw) if pd.notna(raw) else None

        # 11:30 total options volume.
        midday_vol: Optional[float] = None
        midday_mask = df["_ts"].apply(lambda t: (t.hour, t.minute) == _MIDDAY)
        midday_df = df[midday_mask]
        if len(midday_df) > 0:
            ce = pd.to_numeric(
                midday_df["opt_flow_ce_volume_total"].iloc[0]
                if "opt_flow_ce_volume_total" in midday_df.columns else None,
                errors="coerce",
            )
            pe = pd.to_numeric(
                midday_df["opt_flow_pe_volume_total"].iloc[0]
                if "opt_flow_pe_volume_total" in midday_df.columns else None,
                errors="coerce",
            )
            total = ce + pe
            midday_vol = float(total) if pd.notna(total) else None

        day_records.append({"date": date_str, "last_close": last_close, "midday_vol": midday_vol})

    if not day_records:
        return None, None, None

    prev_day_close = day_records[0]["last_close"]
    prev_day_midday_vol = day_records[0]["midday_vol"]

    valid_vols = [r["midday_vol"] for r in day_records[:20] if r["midday_vol"] is not None]
    avg_20d = float(sum(valid_vols) / len(valid_vols)) if valid_vols else None

    return prev_day_close, prev_day_midday_vol, avg_20d


class LiveVelocityAccumulator:
    """
    Stateful per-day accumulator for live velocity features.

    Call ``process(snapshot)`` once per tick inside the live snapshot loop.
    The method is a pure pass-through before 11:30 IST.  At and after 11:30
    it attaches ``snapshot["velocity_enrichment"]`` — a ``Dict[str, float]``
    with the same keys written to ``snapshots_ml_flat_v2`` by the historical
    enrichment pipeline.

    When ``parquet_root`` is supplied, context values (prev_day_close,
    prev_day_midday_option_volume, avg_20d_midday_option_volume) are read from
    the flat-snapshot parquet at day-start.  This closes the training/live gap
    for ctx_gap_*, ctx_am_vol_vs_yday, and vol_spike_ratio.  Without it those
    five features will be NaN (harmless but suboptimal).

    Resets automatically on trade_date boundary.
    """

    def __init__(
        self,
        *,
        parquet_root: Optional[Path] = None,
        source_flat_dataset: str = DEFAULT_SOURCE_FLAT_DATASET,
    ) -> None:
        self._dataset_root: Optional[Path] = (
            Path(parquet_root) / source_flat_dataset if parquet_root is not None else None
        )
        self._morning_rows: List[Dict[str, Any]] = []
        self._velocity: Optional[Dict[str, float]] = None
        self._current_trade_date: Optional[str] = None
        self._computed_for_date: Optional[str] = None
        # Context values loaded from parquet at day-start.
        self._prev_day_close: Optional[float] = None
        self._prev_day_midday_vol: Optional[float] = None
        self._avg_20d_midday_vol: Optional[float] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def process(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Accumulate / compute / inject.  Returns snapshot (possibly with new key)."""
        trade_date = snapshot.get("trade_date") or ""
        ts_str = snapshot.get("timestamp") or ""
        hm = _hm(ts_str)

        if trade_date and trade_date != self._current_trade_date:
            self._reset(trade_date)

        # Accumulate rows inside the morning window (exclude the 11:30 trigger tick
        # itself — it is passed separately as midday_snapshot to compute_velocity_features)
        if _WINDOW_START <= hm < _MIDDAY:
            self._morning_rows.append(_extract_morning_row(snapshot))

        # Compute once at the 11:30 tick
        if hm == _MIDDAY and self._computed_for_date != trade_date:
            self._try_compute(snapshot, trade_date)

        # Inject cached velocity into snapshot (from 11:30 onwards).
        # Key is "velocity_enrichment" — the canonical block name used by
        # stage_views._project_view() when projecting V2 stage-view feature rows.
        if self._velocity is not None:
            snapshot = {**snapshot, "velocity_enrichment": self._velocity}

        return snapshot

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _reset(self, trade_date: str) -> None:
        self._morning_rows = []
        self._velocity = None
        self._current_trade_date = trade_date
        self._prev_day_close = None
        self._prev_day_midday_vol = None
        self._avg_20d_midday_vol = None

        if self._dataset_root is not None:
            try:
                (
                    self._prev_day_close,
                    self._prev_day_midday_vol,
                    self._avg_20d_midday_vol,
                ) = _load_context_for_date(self._dataset_root, trade_date)
                _log.info(
                    "live_velocity: loaded context for %s — "
                    "prev_close=%.2f  prev_midday_vol=%.0f  avg_20d_vol=%.0f",
                    trade_date,
                    self._prev_day_close or float("nan"),
                    self._prev_day_midday_vol or float("nan"),
                    self._avg_20d_midday_vol or float("nan"),
                )
            except Exception as exc:
                _log.warning(
                    "live_velocity: context lookup failed for %s: %s — "
                    "ctx_gap_* / vol_spike_ratio will be NaN",
                    trade_date, exc,
                )

    def _try_compute(self, midday_snapshot: Dict[str, Any], trade_date: str) -> None:
        self._computed_for_date = trade_date

        n = len(self._morning_rows)
        if n < 3:
            _log.warning(
                "live_velocity: %d morning rows on %s (need >=3) — "
                "velocity features will be NaN for this day",
                n, trade_date,
            )
            return

        morning_df = pd.DataFrame(self._morning_rows)
        for col in morning_df.columns:
            if col not in ("timestamp", "trade_date"):
                morning_df[col] = pd.to_numeric(morning_df[col], errors="coerce")

        midday_row = pd.Series(_extract_morning_row(midday_snapshot))

        try:
            velocity = compute_velocity_features(
                morning_df,
                midday_snapshot=midday_row,
                prev_day_close=self._prev_day_close,
                prev_day_midday_option_volume=self._prev_day_midday_vol,
                avg_20d_midday_option_volume=self._avg_20d_midday_vol,
            )
            self._velocity = velocity
            n_valid = sum(1 for v in velocity.values() if v == v)  # non-NaN
            _log.info(
                "live_velocity: computed %d velocity features for %s "
                "(%d/%d valid, %d morning rows)",
                len(velocity), trade_date, n_valid, len(velocity), n,
            )
        except Exception as exc:
            _log.error(
                "live_velocity: compute_velocity_features failed for %s: %s",
                trade_date, exc, exc_info=True,
            )
