"""Run the mandatory repricing-wall control (validation.check_repricing_wall)
against a trained option-payoff model's top-decile holdout fires.

Re-scores each top-decile-predicted holdout bar's payoff using entry
premiums sampled 1 and 2 bars AFTER the original fire bar instead of at the
fire itself, holding to the SAME original target exit bar (so the hold
horizon shortens by 1/2 bars correspondingly) -- exactly the test that
would have caught all three prior documented straddle/vertical failures
(see validation.py's module docstring). A model whose apparent edge
survives this is much more likely measuring real, actionable timing skill;
one whose edge collapses or inverts is very likely measuring the option
chain's own repricing of the same signal it detected.

Usage:
    python -m ml_pipeline_2.scripts.run_repricing_wall_control \\
        --model models/nifty_entry_option_payoff_v1.joblib \\
        --training-view-csv .run/training_view/training_view_nifty_option_pnl.csv.gz \\
        --snapshot-parquet-base .data/ml_pipeline/parquet_data \\
        --holdout-start 2026-06-01 --holdout-end 2026-07-31 \\
        --lot-size 65 --horizon-bars 15 --top-decile-frac 0.10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import joblib
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_repricing_wall_control")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.option_payoff_labels import compute_bar_payoff  # noqa: E402
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402


def find_top_decile_fires(
    model_bundle: dict,
    training_view_csv: str,
    *,
    holdout_start: str,
    holdout_end: str,
    top_decile_frac: float = 0.10,
) -> pd.DataFrame:
    """Score the holdout window with the trained model and return the rows
    in the top `top_decile_frac` of predicted payoff -- these are the bars
    the model would actually have us trade, which is what the repricing
    control needs to test (not an arbitrary/all-bars sample)."""
    df = pd.read_csv(training_view_csv)
    df = df[(df["trade_date"] >= holdout_start) & (df["trade_date"] <= holdout_end)].copy()
    features = model_bundle["features"]
    medians = model_bundle["feature_medians"]
    X = df[features].apply(pd.to_numeric, errors="coerce").fillna(medians)
    df["predicted_payoff"] = model_bundle["model"].predict(X)
    cutoff = df["predicted_payoff"].quantile(1.0 - top_decile_frac)
    fires = df[df["predicted_payoff"] >= cutoff].copy()
    log.info(
        "holdout %s..%s: %d rows, top-%.0f%% predicted-payoff cutoff=%.4f -> %d fires",
        holdout_start, holdout_end, len(df), top_decile_frac * 100, cutoff, len(fires),
    )
    return fires


def _bar_index_in_day(snaps: List, hh_mm_ss: str) -> Optional[int]:
    for i, s in enumerate(snaps):
        ts = s.timestamp
        if ts is None:
            continue
        if ts.strftime("%H:%M:%S") == hh_mm_ss:
            return i
    return None


def reprice_fires_at_delays(
    fires: pd.DataFrame,
    *,
    snapshot_parquet_base: str,
    lot_size: int,
    horizon_bars: int,
) -> tuple[list, list, list]:
    """For each fired row, recompute payoff with entry at bar+0 (the
    original label, already in `payoff_score`), bar+1, and bar+2, holding
    to the SAME original target exit bar. Returns three parallel lists
    ready for validation.check_repricing_wall."""
    payoff_at_fire: list = []
    payoff_delay_1: list = []
    payoff_delay_2: list = []

    for trade_date, group in fires.groupby("trade_date"):
        snaps = read_day_snapshots(snapshot_parquet_base, str(trade_date))
        if not snaps:
            continue
        for _, row in group.iterrows():
            idx = _bar_index_in_day(snaps, str(row["time"]))
            if idx is None:
                continue
            exit_idx = idx + horizon_bars
            if exit_idx >= len(snaps):
                continue
            exit_snap = snaps[exit_idx]

            bp0 = compute_bar_payoff(snaps[idx], exit_snap, lot_size=lot_size)
            score0 = bp0.payoff_score if bp0 is not None else None

            score1 = None
            if idx + 1 < len(snaps):
                bp1 = compute_bar_payoff(snaps[idx + 1], exit_snap, lot_size=lot_size)
                score1 = bp1.payoff_score if bp1 is not None else None

            score2 = None
            if idx + 2 < len(snaps):
                bp2 = compute_bar_payoff(snaps[idx + 2], exit_snap, lot_size=lot_size)
                score2 = bp2.payoff_score if bp2 is not None else None

            payoff_at_fire.append(score0)
            payoff_delay_1.append(score1)
            payoff_delay_2.append(score2)

    return payoff_at_fire, payoff_delay_1, payoff_delay_2


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--training-view-csv", required=True)
    ap.add_argument("--snapshot-parquet-base", required=True)
    ap.add_argument("--holdout-start", required=True)
    ap.add_argument("--holdout-end", required=True)
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--top-decile-frac", type=float, default=0.10)
    ap.add_argument("--min-paired-bars", type=int, default=20)
    args = ap.parse_args(argv)

    bundle = joblib.load(args.model)
    fires = find_top_decile_fires(
        bundle, args.training_view_csv,
        holdout_start=args.holdout_start, holdout_end=args.holdout_end,
        top_decile_frac=args.top_decile_frac,
    )
    if len(fires) == 0:
        raise RuntimeError("no fires found in the holdout window -- check the model/CSV/date range")

    payoff_at_fire, payoff_delay_1, payoff_delay_2 = reprice_fires_at_delays(
        fires, snapshot_parquet_base=args.snapshot_parquet_base,
        lot_size=args.lot_size, horizon_bars=args.horizon_bars,
    )

    report = check_repricing_wall(
        payoff_at_fire=payoff_at_fire, payoff_delay_1=payoff_delay_1, payoff_delay_2=payoff_delay_2,
        min_paired_bars=args.min_paired_bars, raise_on_fail=False,
    )
    log.info(
        "REPRICING WALL CONTROL: n_paired=%d mean@fire=%s mean@+1=%s mean@+2=%s p@+1=%s p@+2=%s -- %s",
        report.n_paired_bars, report.mean_payoff_at_fire, report.mean_payoff_delay_1,
        report.mean_payoff_delay_2, report.wilcoxon_p_delay_1, report.wilcoxon_p_delay_2,
        "PASS" if report.ok else "FAIL",
    )
    if report.reason:
        log.info("reason: %s", report.reason)

    out = {
        "n_fires": len(fires),
        "n_paired_bars": report.n_paired_bars,
        "mean_payoff_at_fire": report.mean_payoff_at_fire,
        "mean_payoff_delay_1": report.mean_payoff_delay_1,
        "mean_payoff_delay_2": report.mean_payoff_delay_2,
        "wilcoxon_p_delay_1": report.wilcoxon_p_delay_1,
        "wilcoxon_p_delay_2": report.wilcoxon_p_delay_2,
        "ok": report.ok,
        "reason": report.reason,
    }
    print(json.dumps(out, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
