"""Join the raw resolver-input fields (momentum, VWAP side, VIX change, IV
skew, opening-range state, PCR change) onto the existing NIFTY
option-payoff training view, for the entry-quality-conditioned direction
study (see `ml_pipeline_2.scripts.study_entry_quality_conditioned_direction`
for the study itself).

Why this is a separate build step: `training_view_nifty_option_pnl.csv.gz`
(built by `build_option_pnl_training_view.py`) carries `ENTRY_FEATURES_V3`
(velocity/context/EMA features) plus `payoff_score`/`payoff_winning_side`,
but NOT the raw snapshot fields
`ml_pipeline_2.pipeline.conditional_direction`'s signal-vote functions need
(confirmed 2026-09-27 by inspecting its real header: it has `fut_return_5m`/
`fut_return_15m`/`fut_close`/`vix_intraday_chg` but not `price_vs_vwap`,
`atm_ce_iv`/`atm_pe_iv`, `orh`/`orl`/`orh_broken`/`orl_broken`, or
`pcr_change_5m`). Those have to be re-read from the raw snapshot parquet and
joined on by the same normalized-timestamp key `build_option_pnl_training_view
.join_features_to_labels` already uses (reused here, not re-derived).

Usage:
    python -m ml_pipeline_2.scripts.build_entry_quality_direction_dataset \\
        --training-view-csv .run/training_view/training_view_nifty_option_pnl.csv.gz \\
        --snapshot-parquet-base .data/ml_pipeline/parquet_data \\
        --output .run/training_view/training_view_nifty_direction_condition.csv.gz
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_entry_quality_direction_dataset")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    RAW_SIGNAL_COLUMNS,
    build_raw_signal_frame,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import (  # noqa: E402
    join_features_to_labels,
    read_day_snapshots,
)


def build_raw_signal_dataset(parquet_base: str, trade_dates: List[str]) -> pd.DataFrame:
    """One row per snapshot bar across `trade_dates`, carrying RAW_SIGNAL_COLUMNS
    plus (trade_date, timestamp, bar_index). Days with no parquet data are
    skipped with a warning (matches build_payoff_label_frame's convention),
    not treated as fatal -- a training-view CSV can legitimately reference a
    date this parquet root doesn't have (e.g. a holiday-adjacent edge)."""
    frames = []
    n_found = 0
    for day in trade_dates:
        snaps = read_day_snapshots(parquet_base, day)
        if not snaps:
            log.warning("no snapshots found for trade_date=%s under %s -- skipping", day, parquet_base)
            continue
        n_found += 1
        frames.append(build_raw_signal_frame(snaps))
    if not frames:
        raise RuntimeError(
            f"no snapshots found for ANY of {len(trade_dates)} requested trade_dates under {parquet_base}"
        )
    log.info("read raw signal fields for %d/%d requested trade_dates", n_found, len(trade_dates))
    return pd.concat(frames, ignore_index=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", required=True, help="Output of build_option_pnl_training_view.py")
    ap.add_argument("--snapshot-parquet-base", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)

    base_df = pd.read_csv(args.training_view_csv)
    if "payoff_winning_side" not in base_df.columns:
        raise ValueError(
            f"{args.training_view_csv} has no payoff_winning_side column -- did you pass the "
            f"OUTPUT of build_option_pnl_training_view.py, not the plain feature-only training view?"
        )
    # RAW_SIGNAL_COLUMNS is deliberately restricted to fields NOT already in
    # the training view (see that constant's docstring) -- if any of them DO
    # collide, `join_features_to_labels`'s plain `.merge()` would silently
    # pandas-suffix both copies (`_x`/`_y`) instead of raising, exactly the
    # bug this fail-loud check exists to catch (it previously hid a genuine
    # 100%-covered field behind a bogus "0% coverage" log line).
    colliding = [c for c in RAW_SIGNAL_COLUMNS if c in base_df.columns]
    if colliding:
        raise ValueError(
            f"{args.training_view_csv} already has column(s) {colliding} -- these would collide "
            f"with this script's own raw-signal extraction and get silently pandas-suffixed "
            f"(_x/_y) instead of joining cleanly. Remove them from RAW_SIGNAL_COLUMNS (reuse the "
            f"training view's own copy instead, per entry_quality_direction.py's docstring) rather "
            f"than re-deriving a column the training view already carries."
        )
    trade_dates = sorted(base_df["trade_date"].astype(str).unique())
    log.info("building raw-signal frame for %d trade_dates from %s", len(trade_dates), args.training_view_csv)

    raw_df = build_raw_signal_dataset(args.snapshot_parquet_base, trade_dates)
    merged = join_features_to_labels(base_df, raw_df)
    dropped = len(base_df) - len(merged)
    log.info(
        "joined %d/%d rows to raw signal fields (%d dropped -- no matching raw snapshot at that timestamp)",
        len(merged), len(base_df), dropped,
    )
    for col in RAW_SIGNAL_COLUMNS:
        if col not in merged.columns:
            raise RuntimeError(
                f"expected raw signal column '{col}' missing from the joined output -- "
                f"build_raw_signal_frame/join_features_to_labels contract violated, investigate "
                f"before trusting this dataset."
            )
        coverage = merged[col].notna().mean()
        log.info("raw signal column coverage: %s = %.1f%%", col, coverage * 100)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.output, index=False, compression="gzip" if str(args.output).endswith(".gz") else None)
    log.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
