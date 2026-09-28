"""Leg-split companion to ``study_asymmetric_exit_coinflip`` for BankNifty
(2026-09-28): the SAME entry-timing-filtered bars, the SAME stop/trail grid,
the SAME walk-forward windows -- but instead of a coin-flip CE/PE
assignment, every fired bar is traded as CE ("all-CE") and, separately,
as PE ("all-PE").

Why this exists: the coin-flip study answers "can exit shape alone rescue a
direction-less entry" and blends both legs 50/50. BankNifty's specific,
documented motivation for re-running that study (``docs/archive/
strategy_design/EXIT_RISK_EXPERIMENTS_2026-05.md``: E2 run CE leg PF 1.42 vs
PE leg PF 0.81; Ref run 1.36 vs 0.79; E4 1.27 vs 0.89) is a claimed LEG
asymmetry. A 50/50 blend cannot show whether one leg's win/loss-size
distribution under an asymmetric exit is genuinely different from the
other's -- this study can, at near-zero extra cost, because
``study_asymmetric_exit_coinflip.build_window_entries`` already builds both
legs' full forward premium paths for every fired bar, and
``variable_hold_exit.simulate_exit_grid_batch`` already simulates the full
grid on each leg independently. This module only changes the final side
assignment (all-True / all-False instead of a random draw).

Caveat carried into every report line: "all-CE on every top-decile bar" is
itself a DIRECTIONAL bet (long calls), so a positive all-CE result over a
window where BankNifty trended up would reflect the underlying's drift,
not a structural leg edge. The per-window breakdown and the underlying's
own per-window drift (``fut_close`` first->last of each test window) are
reported side by side for exactly this reason.

Registry-recorded research only. Imports nothing from
``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.risk.manager``.

Usage:
    python -m ml_pipeline_2.scripts.study_asymmetric_exit_leg_split_banknifty \\
        --training-view-csv .run/training_view/training_view_banknifty_option_pnl.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data_banknifty \\
        --lot-size 30 --output-json .run/asymmetric_exit_coinflip/banknifty_leg_split_h15.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("study_asymmetric_exit_leg_split_banknifty")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import build_expanding_windows  # noqa: E402
from ml_pipeline_2.pipeline.joint_direction_threshold import bootstrap_mean_ci, consistency_summary  # noqa: E402
from ml_pipeline_2.pipeline.variable_hold_exit import (  # noqa: E402
    DEFAULT_MAX_BARS,
    DEFAULT_SESSION_END_MIN,
    DEFAULT_STOP_GRID,
    DEFAULT_TRAIL_ACTIVATE_GRID,
    DEFAULT_TRAIL_DISTANCE_GRID,
    ExitGridParams,
    build_exit_grid,
    simulate_exit_grid_batch,
    summarize_returns,
)
from ml_pipeline_2.scripts.study_asymmetric_exit_coinflip import (  # noqa: E402
    _json_safe,
    build_window_entries,
)

LEGS = ("CE", "PE")


def leg_combo_rows(
    per_window_net: Sequence[np.ndarray],
    grid: Sequence[ExitGridParams],
    *,
    recent_n: int = 4,
    min_rows: int = 20,
    n_boot: int = 1000,
) -> List[Dict[str, Any]]:
    """Per-combo cross-window consistency + pooled stats for ONE leg.

    `per_window_net[w]` is that window's (n_entries, n_combos) net-return
    array for this leg (NaN rows = illiquid leg, dropped). Pure -- no
    snapshot / model dependency -- so it is unit-testable with synthetic
    arrays. Reuses ``joint_direction_threshold.consistency_summary`` (same
    "all windows or the most recent N" bar every sibling study uses) and
    ``bootstrap_mean_ci`` for the pooled expectancy's 95% CI."""
    rows: List[Dict[str, Any]] = []
    for k, params in enumerate(grid):
        per_window_rows = []
        pooled_parts = []
        for arr in per_window_net:
            col = np.asarray(arr, dtype=float)[:, k]
            stats = summarize_returns(col)
            per_window_rows.append({
                "mean_actual": stats["expectancy"],
                "positive": bool(stats["expectancy"] is not None and stats["expectancy"] > 0),
                "thin_sample": stats["n"] < min_rows,
            })
            pooled_parts.append(col[~np.isnan(col)])
        pooled = np.concatenate(pooled_parts) if pooled_parts else np.zeros(0)
        pooled_stats = summarize_returns(pooled)
        lo, hi = bootstrap_mean_ci(pooled, n_boot=n_boot) if pooled.size >= 2 else (None, None)
        pooled_stats["expectancy_ci_lo"] = round(lo, 5) if lo is not None else None
        pooled_stats["expectancy_ci_hi"] = round(hi, 5) if hi is not None else None
        wins = pooled[pooled > 0].sum()
        losses = -pooled[pooled <= 0].sum()
        pooled_stats["profit_factor"] = round(float(wins / losses), 4) if losses > 0 else None
        rows.append({
            "combo": params.label,
            "stop_loss_pct": params.stop_loss_pct,
            "trail_activate_pct": params.trail_activate_pct,
            "trail_distance_pct": params.trail_distance_pct,
            "consistency": consistency_summary(per_window_rows, recent_n=recent_n),
            "pooled_stats": pooled_stats,
        })
    return rows


def window_underlying_drift(test_df: pd.DataFrame) -> Optional[float]:
    """First-to-last ``fut_close`` % change over a test window -- the
    directional-drift context an all-CE / all-PE result must be read
    against. None if fut_close is unavailable."""
    if "fut_close" not in test_df.columns or test_df.empty:
        return None
    s = pd.to_numeric(test_df.sort_values(["trade_date", "time"])["fut_close"], errors="coerce").dropna()
    if len(s) < 2 or s.iloc[0] <= 0:
        return None
    return round(float(s.iloc[-1] / s.iloc[0] - 1.0), 5)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=".run/training_view/training_view_banknifty_option_pnl.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data_banknifty")
    ap.add_argument("--lot-size", type=int, default=30)
    ap.add_argument("--n-windows", type=int, default=5)
    ap.add_argument("--min-train-days", type=int, default=200)
    ap.add_argument("--min-test-rows", type=int, default=30)
    ap.add_argument("--entry-top-quantile", type=float, default=0.90)
    ap.add_argument("--max-bars", type=int, default=DEFAULT_MAX_BARS)
    ap.add_argument("--session-end-min", type=int, default=DEFAULT_SESSION_END_MIN)
    ap.add_argument("--recent-n", type=int, default=4)
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    grid = build_exit_grid(DEFAULT_STOP_GRID, DEFAULT_TRAIL_ACTIVATE_GRID, DEFAULT_TRAIL_DISTANCE_GRID)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "time"]).reset_index(drop=True)
    windows = build_expanding_windows(sorted(df["trade_date"].unique().tolist()),
                                      n_windows=args.n_windows, min_train_days=args.min_train_days)

    per_leg_net: Dict[str, List[np.ndarray]] = {"CE": [], "PE": []}
    window_meta: List[Dict[str, Any]] = []
    for w in windows:
        rec = build_window_entries(
            df, w, lot_size=args.lot_size, entry_top_quantile=args.entry_top_quantile,
            parquet_base=args.parquet_base, max_bars=args.max_bars,
            session_end_min=args.session_end_min, min_test_rows=args.min_test_rows,
        )
        test_df = df[(df["trade_date"] >= w.test_start) & (df["trade_date"] <= w.test_end)]
        meta = {"window": w.window_id, "test_start": w.test_start, "test_end": w.test_end,
                "status": rec.get("status"), "n_fired": rec.get("n_fired_entries"),
                "underlying_drift_pct": window_underlying_drift(test_df)}
        window_meta.append(meta)
        log.info("window %d status=%s n_fired=%s drift=%s", w.window_id, meta["status"], meta["n_fired"], meta["underlying_drift_pct"])
        if rec.get("status") != "ok":
            continue
        for leg in LEGS:
            s = leg.lower()
            out = simulate_exit_grid_batch(rec[f"_entry_premium_{s}"], rec[f"_premium_paths_{s}"],
                                           rec[f"_path_lens_{s}"], rec[f"_cost_pct_{s}"], grid)
            per_leg_net[leg].append(out.net_return_pct)

    results = {leg: leg_combo_rows(per_leg_net[leg], grid, recent_n=args.recent_n) for leg in LEGS}

    print("\n" + "=" * 78)
    print("=== LEG SPLIT: same top-decile bars, every bar traded as CE / as PE ===")
    print("=" * 78)
    print("per-window underlying drift (fut_close first->last): "
          + ", ".join(f"w{m['window']}={m['underlying_drift_pct']}" for m in window_meta))
    for leg in LEGS:
        rows = sorted(results[leg], key=lambda r: -(r["pooled_stats"]["expectancy"] or -999))
        n_consistent = sum(1 for r in rows if r["consistency"]["all_windows_positive"] or r["consistency"]["recent_all_positive"])
        n_pooled_pos = sum(1 for r in rows if (r["pooled_stats"]["expectancy"] or -1) > 0)
        print(f"\n--- ALL-{leg}: {n_consistent}/{len(rows)} combos consistently positive, "
              f"{n_pooled_pos}/{len(rows)} pooled-positive ---")
        for r in rows[:8]:
            p = r["pooled_stats"]
            print(f"  {r['combo']:<30} exp={p['expectancy']} CI=[{p['expectancy_ci_lo']},{p['expectancy_ci_hi']}] "
                  f"PF={p['profit_factor']} win={p['win_rate']} mean_win={p['mean_win']} mean_loss={p['mean_loss']} "
                  f"by_window={r['consistency']['means_by_window']}")

    report = {"config": vars(args), "windows": window_meta, "legs": _json_safe(results)}
    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
