"""Join per-leg (CE/PE) raw net returns and leg-specific raw features onto
the existing NIFTY option-payoff training view, for the direction-margin
research track (see
``ml_pipeline_2.pipeline.direction_margin``'s module docstring for the full
methodology background).

Why this is a separate build step (same reasoning as
``build_entry_quality_direction_dataset.py``'s own docstring for a
different field set): ``training_view_nifty_option_pnl.csv.gz`` (built by
``build_option_pnl_training_view.py``) carries ``ENTRY_FEATURES_V3`` plus
``payoff_score`` (``max(net_ce_return, net_pe_return)``) and
``payoff_winning_side``, but NOT the two underlying per-leg values
SEPARATELY, and not the raw per-leg LEVEL fields (IV, OI, volume, OI
change, volume ratio) ``strategy_app/market/snapshot_accessor.py`` exposes
directly. Both have to be recomputed from the raw snapshot parquet (the
SAME source ``build_option_pnl_training_view.read_day_snapshots`` already
reads) and joined on by the same normalized-timestamp key
``join_features_to_labels`` already uses -- reused here, not re-derived.

Usage:
    python -m ml_pipeline_2.scripts.build_direction_margin_training_view \\
        --training-view-csv .run/training_view/training_view_nifty_option_pnl.csv.gz \\
        --snapshot-parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --output .run/training_view/training_view_nifty_direction_margin.csv.gz

Run BEFORE trusting the output: the per-leg coverage log line (what
fraction of rows have BOTH legs usable vs one-leg-only) and the consistency
check against the existing ``payoff_score``/``payoff_winning_side`` columns
(should match almost exactly, since both are derived from the identical
``compute_bar_payoff`` call with the same lot_size/horizon_bars -- a
mismatch rate above a rounding-noise trickle means the two builds used
different lot_size/horizon_bars/parquet snapshots and should not be
trusted together).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_direction_margin_training_view")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.direction_margin import (  # noqa: E402
    LEG_RAW_FEATURE_COLUMNS,
    extract_leg_raw_row,
)
from ml_pipeline_2.pipeline.option_payoff_labels import (  # noqa: E402
    BarPayoff,
    label_day,
    measure_strike_coverage,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import (  # noqa: E402
    join_features_to_labels,
    read_day_snapshots,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor  # noqa: E402


def _margin_rows_for_day(
    snaps: Sequence[SnapshotAccessor],
    labels: Sequence[Optional[BarPayoff]],
) -> List[dict]:
    rows: List[dict] = []
    for i, (snap, bp) in enumerate(zip(snaps, labels)):
        if bp is None or (bp.ce is None and bp.pe is None):
            continue
        ts = snap.timestamp
        if ts is None:
            continue
        raw_row = extract_leg_raw_row(snap, bar_index=i)
        row = {
            "trade_date": raw_row["trade_date"],
            "timestamp": raw_row["timestamp"],
            "bar_index": raw_row["bar_index"],
            "net_ce_return": bp.ce.net_return if bp.ce is not None else None,
            "net_pe_return": bp.pe.net_return if bp.pe is not None else None,
        }
        for col in LEG_RAW_FEATURE_COLUMNS:
            row[col] = raw_row[col]
        rows.append(row)
    return rows


def build_margin_label_frame(
    parquet_base: str,
    trade_dates: Sequence[str],
    *,
    lot_size: int,
    horizon_bars: int,
) -> pd.DataFrame:
    """One row per bar with at least one usable leg, across all
    `trade_dates`. Rows where NEITHER leg has a usable quote are DROPPED,
    never imputed (matches every other label in this pipeline) -- but
    unlike `build_option_pnl_training_view.build_payoff_label_frame`, a row
    with only ONE usable leg is KEPT here (net_ce_return or net_pe_return
    is None on that row), since each of the two independent models only
    needs ITS OWN leg's target to be trainable on that row."""
    rows: List[dict] = []
    all_labels: List[Optional[BarPayoff]] = []
    days_found = 0
    for day in trade_dates:
        snaps = read_day_snapshots(parquet_base, day)
        if not snaps:
            log.warning("no snapshots found for trade_date=%s under %s -- skipping", day, parquet_base)
            continue
        days_found += 1
        labels = label_day(snaps, horizon_bars=horizon_bars, lot_size=lot_size)
        all_labels.extend(labels)
        rows.extend(_margin_rows_for_day(snaps, labels))

    if not all_labels:
        raise RuntimeError(
            f"no snapshots found for ANY of {len(trade_dates)} requested trade_dates under "
            f"{parquet_base}"
        )

    coverage = measure_strike_coverage(all_labels)
    log.info(
        "strike coverage over %d days (%d had data): %d/%d bars had a resolvable ATM strike, "
        "missing_quote_rate=%.3f (gate: <=0.30 %s)",
        len(trade_dates), days_found,
        coverage.bars_with_atm_strike, coverage.total_entry_bars, coverage.missing_quote_rate,
        "PASS" if coverage.passes_missing_quote_gate else "FAIL",
    )
    if not coverage.passes_missing_quote_gate:
        log.warning(
            "missing_quote_rate gate FAILED (%.3f > 0.30) -- do not train on this output without "
            "investigating first.", coverage.missing_quote_rate,
        )

    df = pd.DataFrame(rows)
    n = len(df)
    if n:
        both = int((df["net_ce_return"].notna() & df["net_pe_return"].notna()).sum())
        ce_only = int((df["net_ce_return"].notna() & df["net_pe_return"].isna()).sum())
        pe_only = int((df["net_ce_return"].isna() & df["net_pe_return"].notna()).sum())
        log.info(
            "per-leg coverage: %d rows total, both-legs-usable=%d (%.3f), CE-only=%d (%.3f), "
            "PE-only=%d (%.3f)",
            n, both, both / n, ce_only, ce_only / n, pe_only, pe_only / n,
        )
    return df


def _log_consistency_check(merged: pd.DataFrame) -> None:
    """Sanity check against the base training view's own payoff_score /
    payoff_winning_side (see module docstring) -- both are derived from an
    identical compute_bar_payoff call, so this should match almost exactly
    if lot_size/horizon_bars/parquet source are all consistent."""
    if "payoff_score" not in merged.columns or "payoff_winning_side" not in merged.columns:
        log.info("base training view has no payoff_score/payoff_winning_side columns -- skipping consistency check")
        return
    both = merged[["net_ce_return", "net_pe_return"]]
    computed_max = both.max(axis=1, skipna=True)
    score_diff = (computed_max - merged["payoff_score"]).abs()
    n_checked = int(score_diff.notna().sum())
    n_mismatch = int((score_diff > 1e-6).sum())
    log.info(
        "consistency check vs existing payoff_score: %d/%d rows match within 1e-6 (%d mismatches)",
        n_checked - n_mismatch, n_checked, n_mismatch,
    )
    ce = pd.to_numeric(merged["net_ce_return"], errors="coerce")
    pe = pd.to_numeric(merged["net_pe_return"], errors="coerce")
    computed_side = np.where(
        ce.notna() & (pe.isna() | (ce >= pe)), "CE",
        np.where(pe.notna(), "PE", None),
    )
    side_mask = merged["payoff_winning_side"].notna() & (ce.notna() | pe.notna())
    n_side_checked = int(side_mask.sum())
    n_side_mismatch = int((computed_side[side_mask.to_numpy()] != merged.loc[side_mask, "payoff_winning_side"].to_numpy()).sum())
    log.info(
        "consistency check vs existing payoff_winning_side: %d/%d rows match (%d mismatches)",
        n_side_checked - n_side_mismatch, n_side_checked, n_side_mismatch,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", required=True, help="Existing training_view_nifty_option_pnl.csv.gz")
    ap.add_argument("--snapshot-parquet-base", required=True, help="Raw historical snapshot parquet root")
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--horizon-bars", type=int, default=15, help="Bar-index hold horizon (NIFTY: 15, ~1min bars)")
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    base_df = pd.read_csv(args.training_view_csv)
    if "payoff_winning_side" not in base_df.columns:
        raise ValueError(
            f"{args.training_view_csv} has no payoff_winning_side column -- did you pass the "
            f"OUTPUT of build_option_pnl_training_view.py, not the plain feature-only training view?"
        )
    # NEW_LABEL_COLUMNS is deliberately restricted to fields NOT already in the
    # training view -- if any DO collide, join_features_to_labels's plain
    # .merge() would silently pandas-suffix (_x/_y) instead of joining cleanly
    # (the exact real bug documented in entry_quality_direction.py's docstring
    # for a different field set). Check explicitly rather than risk repeating it.
    new_label_columns = ["bar_index", "net_ce_return", "net_pe_return"] + LEG_RAW_FEATURE_COLUMNS
    colliding = [c for c in new_label_columns if c in base_df.columns]
    if colliding:
        raise ValueError(
            f"{args.training_view_csv} already has column(s) {colliding} -- these would collide "
            f"with this script's own per-leg extraction and get silently pandas-suffixed (_x/_y) "
            f"instead of joining cleanly. Remove them from LEG_RAW_FEATURE_COLUMNS (reuse the "
            f"training view's own copy instead) rather than re-deriving a column it already carries."
        )
    trade_dates = sorted(base_df["trade_date"].astype(str).unique())
    log.info("building direction-margin labels for %d trade_dates from %s", len(trade_dates), args.training_view_csv)

    label_df = build_margin_label_frame(
        args.snapshot_parquet_base, trade_dates, lot_size=args.lot_size, horizon_bars=args.horizon_bars,
    )
    merged = join_features_to_labels(base_df, label_df)
    dropped = len(base_df) - len(merged)
    log.info(
        "joined %d/%d feature rows to a direction-margin label (%d dropped -- no usable leg, or outside session)",
        len(merged), len(base_df), dropped,
    )
    _log_consistency_check(merged)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.output, index=False, compression="gzip" if str(args.output).endswith(".gz") else None)
    log.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
