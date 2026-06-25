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
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from snapshot_app.core.velocity_features import (
    compute_velocity_features,
    compute_per_bar_velocity_df,
    VELOCITY_COLUMNS as _VELOCITY_COLS,
)

_VELOCITY_OUTPUT_SET: frozenset = frozenset(_VELOCITY_COLS)

_log = logging.getLogger(__name__)

# Accumulate from session open — velocity valid from 9:45 (30 bars of warmup).
# No 11:30 restriction: compute_per_bar_velocity_df fires at every bar.
_WINDOW_START: Tuple[int, int] = (9, 15)
_MIDDAY: Tuple[int, int] = (11, 30)   # kept for context-loader backward compat

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


# ───────────────────────────────────────────────────────────────────────────
# Mongo-backed context loader (live runtime has the raw snapshots in mongo but
# no flat parquet). Mirrors _load_context_for_date semantics EXACTLY so live
# values match the training-data ones:
#   - prev_day_close            = last-of-day futures_bar.fut_close (prev day)
#   - prev_day_midday_vol       = (total_ce_volume + total_pe_volume) at 11:30
#   - avg_20d_midday_vol        = 20-day average of the 11:30 totals
# The 11:30 total (ce+pe) matches velocity_features' current_midday_option_volume
# numerator, so vol_spike_ratio is dimensionally consistent.
# ───────────────────────────────────────────────────────────────────────────

# snapshot_id format is "YYYYMMDD_HHMM"; the 11:30 IST bar ends "_1130".
_MIDDAY_SID_SUFFIX = "_1130"


def _snap_block(doc: Dict[str, Any], block: str) -> Dict[str, Any]:
    """Return payload.snapshot.<block> (or payload.<block>) as a dict, safely."""
    payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else doc
    snap = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
    blk = snap.get(block) if isinstance(snap, dict) else None
    return blk if isinstance(blk, dict) else {}


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # reject NaN


def _load_context_from_mongo(
    db: Any,
    trade_date: str,
    *,
    collections: Tuple[str, ...],
    lookback: int = 25,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Read prev-day context from mongo snapshot collections.

    `trade_date` is "YYYY-MM-DD". snapshot_id dates are "YYYYMMDD". Only days
    strictly before `trade_date` are used. Returns (None, None, None) when the
    history is unavailable — caller treats that as "leave features NaN".
    """
    target = str(trade_date or "").replace("-", "")
    if len(target) != 8 or not target.isdigit():
        return None, None, None

    # 11:30 bars define the candidate prior days and their midday option volume.
    midday_by_date: Dict[str, float] = {}
    for coll in collections:
        try:
            cursor = db[coll].find(
                {"snapshot_id": {"$regex": _MIDDAY_SID_SUFFIX + "$"}},
                {
                    "snapshot_id": 1,
                    "payload.snapshot.chain_aggregates.total_ce_volume": 1,
                    "payload.snapshot.chain_aggregates.total_pe_volume": 1,
                    "_id": 0,
                },
            )
        except Exception:
            continue
        for doc in cursor:
            sid = str(doc.get("snapshot_id") or "")
            date = sid.split("_", 1)[0]
            if len(date) != 8 or date >= target:
                continue
            if date in midday_by_date:
                continue
            ca = _snap_block(doc, "chain_aggregates")
            ce = _num(ca.get("total_ce_volume"))
            pe = _num(ca.get("total_pe_volume"))
            if ce is not None and pe is not None:
                midday_by_date[date] = ce + pe

    if not midday_by_date:
        return None, None, None

    dates = sorted(midday_by_date.keys(), reverse=True)[:lookback]
    prev_date = dates[0]

    prev_day_close = _last_fut_close_for_date(db, prev_date, collections=collections)
    prev_day_midday_vol = midday_by_date.get(prev_date)

    vols = [midday_by_date[d] for d in dates[:20] if midday_by_date.get(d) is not None]
    avg_20d = float(sum(vols) / len(vols)) if vols else None

    return prev_day_close, prev_day_midday_vol, avg_20d


def _last_fut_close_for_date(
    db: Any,
    date: str,
    *,
    collections: Tuple[str, ...],
) -> Optional[float]:
    """Last-of-day futures close for `date` ("YYYYMMDD") across collections."""
    best_sid = ""
    best_close: Optional[float] = None
    for coll in collections:
        try:
            doc = db[coll].find_one(
                {"snapshot_id": {"$regex": "^" + date + "_"}},
                {"snapshot_id": 1, "payload.snapshot.futures_bar.fut_close": 1, "_id": 0},
                sort=[("snapshot_id", -1)],
            )
        except Exception:
            continue
        if not doc:
            continue
        sid = str(doc.get("snapshot_id") or "")
        close = _num(_snap_block(doc, "futures_bar").get("fut_close"))
        if close is not None and sid > best_sid:
            best_sid = sid
            best_close = close
    return best_close


def make_mongo_context_provider(
    db: Any,
    *,
    collections: Tuple[str, ...] = (
        "phase1_market_snapshots",
        "phase1_market_snapshots_historical",
    ),
    lookback: int = 25,
) -> Callable[[str], Tuple[Optional[float], Optional[float], Optional[float]]]:
    """Build a context_provider for LiveVelocityAccumulator backed by mongo."""

    def _provider(trade_date: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return _load_context_from_mongo(
            db, trade_date, collections=collections, lookback=lookback
        )

    return _provider


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
        context_provider: Optional[
            Callable[[str], Tuple[Optional[float], Optional[float], Optional[float]]]
        ] = None,
    ) -> None:
        self._dataset_root: Optional[Path] = (
            Path(parquet_root) / source_flat_dataset if parquet_root is not None else None
        )
        # Optional injectable provider for (prev_day_close, prev_day_midday_vol,
        # avg_20d_midday_vol). When set it takes precedence over the parquet
        # dataset — used live to read prior-day context from mongo (the runtime
        # VM has no flat parquet). Returning Nones is safe: features stay NaN,
        # i.e. identical to the no-context behaviour (no regression).
        self._context_provider = context_provider
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
        """Accumulate / compute / inject.  Returns snapshot (possibly with new key).

        Per-bar computation from 9:15 (no 11:30 restriction).
        velocity_enrichment is populated from the very first bar onward.
        Uses compute_per_bar_velocity_df — same function as dhan_data_pipeline —
        guaranteeing zero train/serve skew.
        """
        trade_date = snapshot.get("trade_date") or ""
        ts_str = snapshot.get("timestamp") or ""
        hm = _hm(ts_str)

        if trade_date and trade_date != self._current_trade_date:
            self._reset(trade_date)

        # Accumulate every bar from 9:15 onwards (including 11:30 and beyond)
        if hm >= _WINDOW_START:
            self._morning_rows.append(_extract_morning_row(snapshot))

        # Compute per-bar from 9:15 (need ≥3 rows for meaningful features)
        if len(self._morning_rows) >= 3:
            self._try_compute_per_bar(trade_date)

        # Inject current-bar velocity into snapshot.
        # Key is "velocity_enrichment" — canonical block consumed by stage_views.
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

        # Prefer the injected provider (mongo, live) over the parquet dataset.
        loader: Optional[Callable[[str], Tuple[Optional[float], Optional[float], Optional[float]]]]
        if self._context_provider is not None:
            loader = self._context_provider
            source = "provider"
        elif self._dataset_root is not None:
            loader = lambda td: _load_context_for_date(self._dataset_root, td)  # noqa: E731
            source = "parquet"
        else:
            loader = None
            source = "none"

        if loader is not None:
            try:
                (
                    self._prev_day_close,
                    self._prev_day_midday_vol,
                    self._avg_20d_midday_vol,
                ) = loader(trade_date)
                _log.info(
                    "live_velocity: loaded context for %s (src=%s) — "
                    "prev_close=%.2f  prev_midday_vol=%.0f  avg_20d_vol=%.0f",
                    trade_date,
                    source,
                    self._prev_day_close if self._prev_day_close is not None else float("nan"),
                    self._prev_day_midday_vol if self._prev_day_midday_vol is not None else float("nan"),
                    self._avg_20d_midday_vol if self._avg_20d_midday_vol is not None else float("nan"),
                )
            except Exception as exc:
                _log.warning(
                    "live_velocity: context lookup failed for %s (src=%s): %s — "
                    "ctx_gap_* / vol_spike_ratio will be NaN",
                    trade_date, source, exc,
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

    def _try_compute_per_bar(self, trade_date: str) -> None:
        """Compute per-bar velocity from 9:15 and store last row as current velocity.

        Called at every bar after accumulating ≥3 rows. Uses the same
        compute_per_bar_velocity_df() function as dhan_data_pipeline — zero skew.
        """
        try:
            accumulated_df = pd.DataFrame(self._morning_rows)
            for col in accumulated_df.columns:
                if col not in ("timestamp", "trade_date"):
                    accumulated_df[col] = pd.to_numeric(accumulated_df[col], errors="coerce")

            enriched = compute_per_bar_velocity_df(
                accumulated_df,
                prev_day_close=self._prev_day_close,
                prev_day_midday_option_volume=self._prev_day_midday_vol,
                avg_20d_midday_option_volume=self._avg_20d_midday_vol,
            )
            # Extract the last row (= current bar) as the velocity dict
            last = enriched.iloc[-1]
            self._velocity = {
                col: float(last[col])
                for col in enriched.columns
                if col in _VELOCITY_OUTPUT_SET
                and pd.notna(last[col])
            }
            # Fill missing output columns with NaN so downstream never KeyErrors
            from snapshot_app.core.velocity_features import _ALL_OUTPUT_COLUMNS as _VCOLS
            for k in _VCOLS:
                if k not in self._velocity:
                    self._velocity[k] = float("nan")

        except Exception as exc:
            _log.debug(
                "live_velocity: per-bar compute failed for %s (%d rows): %s",
                trade_date, len(self._morning_rows), exc,
            )
