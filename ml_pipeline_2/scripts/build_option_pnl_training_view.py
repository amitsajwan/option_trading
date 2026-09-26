"""Join option_payoff_labels.py's cost-aware, direction-agnostic payoff
label onto an existing training-view CSV (the format built by
build_training_view_from_mongo.py), producing an augmented CSV that
train_entry_dhan_v3.py can train a regression model against.

The training-view CSV carries features only -- no per-leg raw option
premiums (build_training_view_from_mongo.py's _VERIFIED_NEW_FEATURES only
keeps atm_ce_iv/atm_pe_iv/atm_straddle_price, not atm_ce_close/atm_pe_close).
So the payoff label is computed separately, from the raw historical
snapshot parquet (the SAME source strategy_app.sim.replay_engine.replay_day
replays -- see ml_pipeline_2.pipeline.backtest_runner._read_sample_instrument
for the identical read pattern), then joined onto the feature rows by
(trade_date, timestamp).

Usage:
    python -m ml_pipeline_2.scripts.build_option_pnl_training_view \\
        --training-view-csv .run/training_view/training_view_nifty.csv.gz \\
        --snapshot-parquet-base /app/.data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --output .run/training_view/training_view_nifty_option_pnl.csv.gz

Run the strike-coverage QA (logged automatically, see the "strike coverage"
log line) BEFORE trusting the output -- a missing_quote_rate above 30% or a
payoff_positive_rate outside 5-60% means the label isn't trustworthy yet
(see ml_pipeline_2/docs/training/OPTION_LABEL_CONTRACT.md's sanity-gate
discipline, which these thresholds are taken from verbatim).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_option_pnl_training_view")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.option_payoff_labels import (  # noqa: E402
    BarPayoff,
    label_day,
    measure_strike_coverage,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor  # noqa: E402


def read_day_snapshots(parquet_base: str, trade_date: str) -> list[SnapshotAccessor]:
    """Read one day's raw snapshots (sorted by timestamp) from the canonical
    `{parquet_base}/snapshots/trade_date=<date>/data.parquet` format --
    same source and read pattern as
    ml_pipeline_2.pipeline.backtest_runner._read_sample_instrument, so this
    label is built from exactly what the SIM harness replays.
    """
    import duckdb

    path = Path(parquet_base) / "snapshots" / f"trade_date={trade_date}" / "data.parquet"
    if not path.exists():
        return []
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT snapshot_raw_json FROM read_parquet('{path.as_posix()}')"
        ).fetchdf()
    finally:
        con.close()
    accessors = []
    for raw in df["snapshot_raw_json"]:
        payload = json.loads(raw)
        accessors.append(SnapshotAccessor(payload))
    accessors.sort(key=lambda a: a.timestamp_or_now)
    return accessors


def _label_rows_for_day(
    snaps: Sequence[SnapshotAccessor],
    labels: Sequence[Optional[BarPayoff]],
) -> list[dict]:
    rows = []
    for snap, bp in zip(snaps, labels):
        if bp is None or bp.payoff_score is None:
            continue
        ts = snap.timestamp
        if ts is None:
            continue
        rows.append(
            {
                "trade_date": snap.trade_date,
                "timestamp": snap.raw_payload.get("timestamp"),  # raw string, for join normalization below
                "payoff_score": bp.payoff_score,
                "payoff_winning_side": bp.winning_side,
                "payoff_strike": bp.strike,
            }
        )
    return rows


def build_payoff_label_frame(
    parquet_base: str,
    trade_dates: Sequence[str],
    *,
    lot_size: int,
    horizon_bars: int,
) -> pd.DataFrame:
    """One row per labelable bar across all `trade_dates`. Rows where the
    label is None (missing quote, illiquid, outside session, near session
    end) are DROPPED, not imputed -- matches every other label in this
    pipeline (OPTION_LABEL_CONTRACT.md clause #10)."""
    rows: list[dict] = []
    all_labels: list[Optional[BarPayoff]] = []
    days_found = 0
    for day in trade_dates:
        snaps = read_day_snapshots(parquet_base, day)
        if not snaps:
            log.warning("no snapshots found for trade_date=%s under %s -- skipping", day, parquet_base)
            continue
        days_found += 1
        labels = label_day(snaps, horizon_bars=horizon_bars, lot_size=lot_size)
        all_labels.extend(labels)
        rows.extend(_label_rows_for_day(snaps, labels))

    if not all_labels:
        raise RuntimeError(
            f"no snapshots found for ANY of {len(trade_dates)} requested trade_dates under "
            f"{parquet_base} -- check --snapshot-parquet-base is correct for this instrument "
            f"(see ml_pipeline_2.pipeline.instrument_config.py's parquet_base_notes)."
        )

    coverage = measure_strike_coverage(all_labels)
    log.info(
        "strike coverage over %d days (%d had data): %d/%d bars had a resolvable ATM strike, "
        "missing_quote_rate=%.3f (gate: <=0.30 %s), payoff_positive_rate=%.3f (gate: 0.05-0.60 %s)",
        len(trade_dates), days_found,
        coverage.bars_with_atm_strike, coverage.total_entry_bars, coverage.missing_quote_rate,
        "PASS" if coverage.passes_missing_quote_gate else "FAIL",
        coverage.payoff_positive_rate,
        "PASS" if coverage.passes_positive_rate_gate else "FAIL",
    )
    if not coverage.passes_missing_quote_gate:
        log.warning(
            "missing_quote_rate gate FAILED (%.3f > 0.30) -- the option chain in this parquet "
            "may be too narrow (e.g. ATM-only, no wider ladder) to trust this label at scale. "
            "Do not train on this output without investigating first.",
        )
    if not coverage.passes_positive_rate_gate:
        log.warning(
            "payoff_positive_rate gate FAILED (%.3f outside 0.05-0.60) -- either too rare to "
            "learn or trivially common/leaky. Do not train on this output without investigating.",
        )
    return pd.DataFrame(rows)


def _normalized_join_key(df: pd.DataFrame) -> pd.Series:
    """Both sides of the join may carry `timestamp` in slightly different
    string representations (the training-view CSV preserves whatever Mongo
    gave it; this script emits SnapshotAccessor's own raw payload string) --
    compare by parsed, UTC-normalized datetime rather than raw string
    equality, so a formatting difference doesn't silently produce a
    zero-row join."""
    return pd.to_datetime(df["timestamp"], utc=True, errors="coerce")


def join_features_to_labels(feature_df: pd.DataFrame, label_df: pd.DataFrame) -> pd.DataFrame:
    left = feature_df.copy()
    right = label_df.copy()
    left["_join_ts"] = _normalized_join_key(left)
    right["_join_ts"] = _normalized_join_key(right)
    right = right.drop(columns=["timestamp", "trade_date"])
    merged = left.merge(right, on="_join_ts", how="inner")
    return merged.drop(columns=["_join_ts"])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", required=True, help="Existing feature-only training view CSV")
    ap.add_argument("--snapshot-parquet-base", required=True, help="Raw historical snapshot parquet root")
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--horizon-bars", type=int, default=15, help="Bar-index hold horizon (NIFTY: 15, ~1min bars)")
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    base_df = pd.read_csv(args.training_view_csv)
    trade_dates = sorted(base_df["trade_date"].astype(str).unique())
    log.info("building option-payoff labels for %d trade_dates from %s", len(trade_dates), args.training_view_csv)

    label_df = build_payoff_label_frame(
        args.snapshot_parquet_base, trade_dates, lot_size=args.lot_size, horizon_bars=args.horizon_bars,
    )
    merged = join_features_to_labels(base_df, label_df)
    dropped = len(base_df) - len(merged)
    log.info(
        "joined %d/%d feature rows to a payoff label (%d dropped -- no label, illiquid, or outside session)",
        len(merged), len(base_df), dropped,
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.output, index=False, compression="gzip" if str(args.output).endswith(".gz") else None)
    log.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
