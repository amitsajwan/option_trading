"""CLI: sanity-check an instrument's real ATM option-premium levels and
intraday move-size distribution against the asymmetric-exit study's
percentage grid (BankNifty research, 2026-09-27) -- see
``ml_pipeline_2.pipeline.premium_profile``'s module docstring for the full
motivation.

Directly answers Part 2 of the BankNifty asymmetric-exit task ("consider
whether the SAME percentage grid still makes sense... sanity-check typical
BankNifty ATM premium levels/volatility first"): BankNifty's absolute point
moves and premium levels differ from NIFTY's, so before reusing NIFTY's
exact ``variable_hold_exit.DEFAULT_STOP_GRID`` /
``DEFAULT_TRAIL_ACTIVATE_GRID`` verbatim, this script measures BankNifty's
OWN real max-favorable / max-adverse intraday premium-move distribution (as
a % of entry premium -- the SAME normalized units the exit grid already
uses) and reports how often each candidate stop/activate level would
actually have been reached. A grid where almost nothing ever reaches the
loosest levels (every combination degenerating to a time-cap exit) or
where the tightest levels are breached on nearly every entry would be
poorly scaled for this instrument and should be adjusted BEFORE running
the full walk-forward study, not after.

Sampling: every `--day-stride`-th trade_date (spread across the WHOLE
available history, not just a recent slice -- this is a volatility-regime
sanity check, not a walk-forward-safe model, so there is no leakage concern
in sampling non-contiguous days) and every `--bar-stride`-th in-session bar
on each sampled day, both CE and PE legs. Reuses
``variable_hold_exit.entry_leg_info`` / ``build_forward_premium_path`` for
the actual forward-path construction (same liquidity floors, same
fixed-contract discipline, same forward-fill convention as the real study)
-- this script measures nothing the real study doesn't already compute
internally, it just aggregates it for a decision made BEFORE that study
runs.

Usage:
    python -m ml_pipeline_2.scripts.check_option_premium_profile \\
        --training-view-csv .run/training_view/training_view_banknifty_option_pnl.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data_banknifty \\
        --lot-size 30 --day-stride 5 --bar-stride 3 --max-bars 90
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("check_option_premium_profile")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.premium_profile import (  # noqa: E402
    entry_premium_level_summary,
    path_extremes,
    summarize_extremes,
)
from ml_pipeline_2.pipeline.variable_hold_exit import (  # noqa: E402
    DEFAULT_MAX_BARS,
    DEFAULT_SESSION_END_MIN,
    DEFAULT_STOP_GRID,
    DEFAULT_TRAIL_ACTIVATE_GRID,
    build_forward_premium_path,
    entry_leg_info,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402


def sample_trade_dates(all_dates: List[str], *, day_stride: int) -> List[str]:
    """Every `day_stride`-th date across the FULL sorted range -- spreads
    the sample across the whole history's volatility regimes rather than
    clustering in one period. Always keeps at least the first date, and
    `day_stride<=1` keeps every date."""
    if day_stride <= 1:
        return list(all_dates)
    return all_dates[::day_stride]


def profile_one_day(
    snaps: List[Any],
    *,
    lot_size: int,
    bar_stride: int,
    max_bars: int,
    session_end_min: int,
) -> Dict[str, List[Dict[str, float]]]:
    """CE/PE path-extremes rows plus entry-premium levels for every
    `bar_stride`-th bar of one day's chronological snapshots that has a
    usable ATM strike and at least one forward bar."""
    ce_extremes: List[Dict[str, float]] = []
    pe_extremes: List[Dict[str, float]] = []
    ce_entry_premiums: List[float] = []
    pe_entry_premiums: List[float] = []
    for idx in range(0, len(snaps), max(1, bar_stride)):
        entry_snap = snaps[idx]
        ce_info = entry_leg_info(entry_snap, "CE", lot_size=lot_size)
        if ce_info is not None:
            fp = build_forward_premium_path(
                snaps, idx, "CE", ce_info.strike, ce_info.entry_premium,
                max_bars=max_bars, session_end_min=session_end_min,
            )
            if fp.n_real > 0:
                ce_extremes.append(path_extremes(ce_info.entry_premium, fp.premiums[: fp.n_real]))
                ce_entry_premiums.append(ce_info.entry_premium)
        pe_info = entry_leg_info(entry_snap, "PE", lot_size=lot_size)
        if pe_info is not None:
            fp = build_forward_premium_path(
                snaps, idx, "PE", pe_info.strike, pe_info.entry_premium,
                max_bars=max_bars, session_end_min=session_end_min,
            )
            if fp.n_real > 0:
                pe_extremes.append(path_extremes(pe_info.entry_premium, fp.premiums[: fp.n_real]))
                pe_entry_premiums.append(pe_info.entry_premium)
    return {
        "ce_extremes": ce_extremes, "pe_extremes": pe_extremes,
        "ce_entry_premiums": ce_entry_premiums, "pe_entry_premiums": pe_entry_premiums,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", required=True, help="Used only to enumerate available trade_dates")
    ap.add_argument("--parquet-base", required=True)
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--day-stride", type=int, default=5)
    ap.add_argument("--bar-stride", type=int, default=3)
    ap.add_argument("--max-bars", type=int, default=DEFAULT_MAX_BARS)
    ap.add_argument("--session-end-min", type=int, default=DEFAULT_SESSION_END_MIN)
    ap.add_argument("--stop-grid", default=",".join(str(x) for x in DEFAULT_STOP_GRID))
    ap.add_argument("--activate-grid", default=",".join(str(x) for x in DEFAULT_TRAIL_ACTIVATE_GRID))
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    stop_grid = [float(x) for x in args.stop_grid.split(",") if x.strip()]
    activate_grid = [float(x) for x in args.activate_grid.split(",") if x.strip()]

    base_df = pd.read_csv(args.training_view_csv, usecols=["trade_date"])
    all_dates = sorted(base_df["trade_date"].astype(str).unique())
    sampled_dates = sample_trade_dates(all_dates, day_stride=args.day_stride)
    log.info(
        "sampling %d/%d trade_dates (day_stride=%d), bar_stride=%d, range %s..%s",
        len(sampled_dates), len(all_dates), args.day_stride, args.bar_stride,
        all_dates[0], all_dates[-1],
    )

    ce_extremes: List[Dict[str, float]] = []
    pe_extremes: List[Dict[str, float]] = []
    ce_premiums: List[float] = []
    pe_premiums: List[float] = []
    days_used = 0
    for d in sampled_dates:
        snaps = read_day_snapshots(args.parquet_base, d)
        if not snaps:
            continue
        days_used += 1
        day_result = profile_one_day(
            snaps, lot_size=args.lot_size, bar_stride=args.bar_stride,
            max_bars=args.max_bars, session_end_min=args.session_end_min,
        )
        ce_extremes.extend(day_result["ce_extremes"])
        pe_extremes.extend(day_result["pe_extremes"])
        ce_premiums.extend(day_result["ce_entry_premiums"])
        pe_premiums.extend(day_result["pe_entry_premiums"])

    log.info("used %d/%d sampled days (rest had no snapshot data)", days_used, len(sampled_dates))
    log.info("collected %d CE entries, %d PE entries", len(ce_extremes), len(pe_extremes))

    ce_summary = summarize_extremes(ce_extremes, stop_grid=stop_grid, activate_grid=activate_grid)
    pe_summary = summarize_extremes(pe_extremes, stop_grid=stop_grid, activate_grid=activate_grid)
    ce_levels = entry_premium_level_summary(ce_premiums)
    pe_levels = entry_premium_level_summary(pe_premiums)

    report = {
        "config": {
            "training_view_csv": args.training_view_csv, "parquet_base": args.parquet_base,
            "lot_size": args.lot_size, "day_stride": args.day_stride, "bar_stride": args.bar_stride,
            "max_bars": args.max_bars, "stop_grid": stop_grid, "activate_grid": activate_grid,
            "days_sampled": len(sampled_dates), "days_used": days_used,
            "history_start": all_dates[0], "history_end": all_dates[-1],
        },
        "ce_entry_premium_levels": ce_levels,
        "pe_entry_premium_levels": pe_levels,
        "ce_move_profile": ce_summary,
        "pe_move_profile": pe_summary,
    }

    print("\n" + "=" * 78)
    print(f"=== Option premium/move profile, lot_size={args.lot_size} ===")
    print("=" * 78)
    print(f"history: {all_dates[0]}..{all_dates[-1]} ({len(all_dates)} trade_dates), "
          f"sampled {len(sampled_dates)} days (stride={args.day_stride}), {days_used} had data")
    print(f"\nCE entry premium levels (Rs): {ce_levels}")
    print(f"PE entry premium levels (Rs): {pe_levels}")
    print(f"\nCE move profile (n={ce_summary.get('n')}): "
          f"max_gain p50/p90={ce_summary.get('max_gain_pct', {}).get('p50')}/{ce_summary.get('max_gain_pct', {}).get('p90')} "
          f"max_drawdown p50/p10={ce_summary.get('max_drawdown_pct', {}).get('p50')}/{ce_summary.get('max_drawdown_pct', {}).get('p10')}")
    print(f"CE stop_hit_rates: {ce_summary.get('stop_hit_rates')}")
    print(f"CE activate_hit_rates: {ce_summary.get('activate_hit_rates')}")
    print(f"\nPE move profile (n={pe_summary.get('n')}): "
          f"max_gain p50/p90={pe_summary.get('max_gain_pct', {}).get('p50')}/{pe_summary.get('max_gain_pct', {}).get('p90')} "
          f"max_drawdown p50/p10={pe_summary.get('max_drawdown_pct', {}).get('p50')}/{pe_summary.get('max_drawdown_pct', {}).get('p10')}")
    print(f"PE stop_hit_rates: {pe_summary.get('stop_hit_rates')}")
    print(f"PE activate_hit_rates: {pe_summary.get('activate_hit_rates')}")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
