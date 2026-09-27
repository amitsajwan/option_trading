"""Walk-forward study: does an ASYMMETRIC exit rule (tight capped hard stop
+ activated trailing profit stop) produce positive expectancy on NIFTY
option trades, even under a literal coin-flip direction call (NIFTY,
2026-09-27)?

This is a fundamentally different lever from every direction-prediction
track this project has already run and closed out near a 50-56% coin flip
(see ``ml_pipeline_2.pipeline.conditional_direction``'s module docstring for
the full list). It does not try to call CE-vs-PE better; it asks whether
the WIN/LOSS SIZE ASYMMETRY of a stop/trailing exit shape alone can produce
positive expectancy despite a ~50% win rate. Motivating, NOT encouraging,
prior evidence: ``strategy_app/senses/cost_ev.py``'s own empirically-anchored
numbers describe the CURRENT live exit logic as having the wrong-shaped
asymmetry (wrong-side hold -7 to -8% > right-side hold +3-5%) -- this study
tests, empirically, whether a deliberately different stop/trailing shape can
flip that the other way, without assuming the answer.

Pipeline per walk-forward window (``conditional_direction.build_expanding
_windows``, same 5-window convention every direction track today used):

1. Fit the entry-timing regressor (``entry_quality_direction
   .fit_entry_payoff_model``, the SAME architecture as
   ``train_entry_option_payoff_v1.py``, no HPO -- matches
   ``study_joint_direction_call_threshold.py``'s own use of this exact
   utility for the identical purpose) on the window's OWN train slice only,
   score the window's OWN test slice, and take the top
   ``--entry-top-quantile`` (default 0.90 = top decile) of ITS OWN
   predictions (``joint_direction_threshold.entry_quality_mask``, imported
   not re-derived) -- these are the bars an independently-validated
   entry-timing signal says are worth trading; walk-forward-safe (no row
   past `window.train_end` touches the fit).
2. For every fired bar, build the FULL forward premium path (up to
   ``--max-bars`` / session end) for BOTH CE and PE at the entry's own,
   fixed ATM strike (``variable_hold_exit.build_forward_premium_path`` /
   ``entry_leg_info`` -- same liquidity floors and fixed-contract
   discipline as ``option_payoff_labels.compute_bar_payoff``), and run the
   full stop/trailing grid on BOTH sides ONCE
   (``variable_hold_exit.simulate_exit_grid_batch``).
3. **Primary test**: assign each fired bar's direction with a fair,
   reproducible coin flip (``variable_hold_exit.assign_random_sides``) --
   a DIFFERENT seed per window (``--base-seed`` + window id, both logged),
   plus 2-3 additional seeds tried on one designated window
   (``--extra-seed-window``, default: the most recent/largest window) to
   confirm any apparent result isn't a seed artifact (this project was
   burned by exactly that today -- an unseeded HPO run flipped a result's
   sign, see ``0f44ef924a``). Report win rate / mean win / mean loss /
   expectancy for EVERY grid combination, per window and pooled.
4. **Whipsaw diagnostic**: for the coin-flip-selected leg, and each hard
   stop level in isolation (trailing disabled),
   ``variable_hold_exit.hard_stop_whipsaw_report`` answers "of the
   positions this stop level cut, what fraction would have recovered to
   net-profitable by the time cap if NOT stopped out" -- the direct,
   mandatory check against this project's own documented naive-stop
   failure (``project_stop_check_intrabar_blindspot_2026-09-12.md``: a
   tighter intrabar-low check flipped 13 real BankNifty winners into -3%
   losses). A high whipsaw rate at a given stop level means that stop is
   shaking out trades that would have worked.
5. **Secondary test**: repeat with the reduced rule-based composite
   direction signal (``conditional_direction.composite_vote`` -- the
   simpler of the two options the owning task allows, chosen over refitting
   the joint multiclass model per window because that model's own Optuna
   HPO refit is expensive and, per today's ``study_joint_direction_call
   _threshold.py`` track, was not itself a clean, single validated
   artifact this study could reuse without repeating that HPO -- a
   reasoned scope cut, not an oversight; see the run's own summary output
   for the explicit note). Where the composite doesn't fire (no strong
   signal that bar), falls back to the SAME per-window coin-flip
   assignment used in the primary test, so the two tests are directly
   comparable on the same fired-bar population; composite fire-rate
   ("coverage") is reported alongside so a thin-firing signal doesn't read
   as more decisive than it is.

Import-isolation constraint (matches every sibling direction/exit-research
module in this pipeline): must NEVER import
``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.risk.manager``,
``strategy_app.brain.regime_director``, or ``strategy_app.market.regime``,
must NEVER touch ``.env.compose``/``docker-compose*.yml``/any deployed model
bundle/``verified_candidates.py``. Registry-recorded research only --
nothing here is live-reachable, no resume/deploy decision is made by this
script.

Usage:
    python -m ml_pipeline_2.scripts.study_asymmetric_exit_coinflip \\
        --training-view-csv .run/training_view/training_view_nifty_option_pnl.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --n-windows 5 --min-train-days 200 \\
        --entry-top-quantile 0.90 --max-bars 90 \\
        --output-json .run/asymmetric_exit_coinflip/nifty_asymmetric_exit_report.json
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
log = logging.getLogger("study_asymmetric_exit_coinflip")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
    composite_vote,
)
from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    build_raw_signal_frame,
    fit_entry_payoff_model,
    score_entry_payoff,
)
from ml_pipeline_2.pipeline.joint_direction_threshold import (  # noqa: E402
    consistency_summary,
    entry_quality_mask,
)
from ml_pipeline_2.pipeline.variable_hold_exit import (  # noqa: E402
    DEFAULT_MAX_BARS,
    DEFAULT_SESSION_END_MIN,
    DEFAULT_STOP_GRID,
    DEFAULT_TRAIL_ACTIVATE_GRID,
    DEFAULT_TRAIL_DISTANCE_GRID,
    ExitGridParams,
    ForwardPath,
    assign_random_sides,
    build_exit_grid,
    build_forward_premium_path,
    entry_leg_info,
    hard_stop_whipsaw_report,
    pad_premium_paths,
    select_inputs_by_side,
    select_outcome_by_side,
    simulate_exit_grid_batch,
    summarize_returns,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402
from ml_pipeline_2.scripts.run_repricing_wall_control import _bar_index_in_day  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_MIN_TEST_ROWS = 30
DEFAULT_ENTRY_TOP_QUANTILE = 0.90
DEFAULT_RECENT_N = 4
DEFAULT_MIN_ROWS_PER_COMBO = 20
# Deliberately dated/documented, not a "random" magic number -- lets anyone
# re-derive exactly which run produced a given seed.
DEFAULT_BASE_SEED = 20260927
DEFAULT_EXTRA_SEEDS: List[int] = [1, 2, 3]

COMPOSITE_RAW_COLUMNS = [
    "price_vs_vwap", "atm_ce_iv", "atm_pe_iv", "orh", "orl", "orh_broken", "orl_broken", "pcr_change_5m",
]


# ── Per-window entry construction ───────────────────────────────────────────

def build_window_entries(
    df: pd.DataFrame,
    window: WalkForwardWindow,
    *,
    lot_size: int,
    entry_top_quantile: float,
    parquet_base: str,
    max_bars: int,
    session_end_min: int,
    min_test_rows: int,
) -> Dict[str, Any]:
    """Fit the entry-timing model on this window's own train slice, score
    its own test slice, take the top-quantile fired bars, and build BOTH
    sides' full forward premium paths for every fired bar (walk-forward
    safe -- see module docstring). Returns a dict with `status` in
    {"ok", "thin_sample"}; "ok" results carry underscore-prefixed keys with
    the raw numpy arrays the grid sweep needs (stripped before JSON
    serialization by `_json_safe`)."""
    train_df = df[df["trade_date"] <= window.train_end].reset_index(drop=True)
    test_df = df[
        (df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)
    ].sort_values(["trade_date", "time"]).reset_index(drop=True)
    result: Dict[str, Any] = {
        "window": window.window_id, "train_start": window.train_start, "train_end": window.train_end,
        "test_start": window.test_start, "test_end": window.test_end,
        "n_train": len(train_df), "n_test": len(test_df),
    }
    if len(test_df) < min_test_rows:
        result["status"] = "thin_sample"
        result["reason"] = f"only {len(test_df)} test rows in this window"
        return result

    model, medians = fit_entry_payoff_model(train_df, list(ENTRY_FEATURES_V3), "payoff_score")
    predicted = score_entry_payoff(model, medians, test_df, list(ENTRY_FEATURES_V3))
    mask = entry_quality_mask(predicted, entry_top_quantile)
    entries = test_df[mask].reset_index(drop=True)
    n = len(entries)
    result["n_fired_entries"] = n
    if n < min_test_rows:
        result["status"] = "thin_sample"
        result["reason"] = f"only {n} top-quantile ({entry_top_quantile}) entries in this window"
        return result

    entry_premium_ce = np.full(n, np.nan)
    entry_premium_pe = np.full(n, np.nan)
    cost_pct_ce = np.full(n, np.nan)
    cost_pct_pe = np.full(n, np.nan)
    forward_ce: List[ForwardPath] = [ForwardPath(np.asarray([]), 0) for _ in range(n)]
    forward_pe: List[ForwardPath] = [ForwardPath(np.asarray([]), 0) for _ in range(n)]
    raw_signal_rows: List[Optional[Dict[str, Any]]] = [None] * n
    n_no_snapshot_day = 0
    n_bar_not_found = 0

    for trade_date, group in entries.groupby("trade_date", sort=False):
        snaps = read_day_snapshots(parquet_base, str(trade_date))
        if not snaps:
            n_no_snapshot_day += len(group)
            continue
        raw_signal_day = build_raw_signal_frame(snaps).set_index("bar_index")
        for pos in group.index:
            row = entries.loc[pos]
            idx = _bar_index_in_day(snaps, str(row["time"]))
            if idx is None:
                n_bar_not_found += 1
                continue
            entry_snap = snaps[idx]
            ce_info = entry_leg_info(entry_snap, "CE", lot_size=lot_size)
            if ce_info is not None:
                fp = build_forward_premium_path(
                    snaps, idx, "CE", ce_info.strike, ce_info.entry_premium,
                    max_bars=max_bars, session_end_min=session_end_min,
                )
                if fp.n_real > 0:
                    entry_premium_ce[pos] = ce_info.entry_premium
                    cost_pct_ce[pos] = ce_info.cost_pct
                    forward_ce[pos] = fp
            pe_info = entry_leg_info(entry_snap, "PE", lot_size=lot_size)
            if pe_info is not None:
                fp = build_forward_premium_path(
                    snaps, idx, "PE", pe_info.strike, pe_info.entry_premium,
                    max_bars=max_bars, session_end_min=session_end_min,
                )
                if fp.n_real > 0:
                    entry_premium_pe[pos] = pe_info.entry_premium
                    cost_pct_pe[pos] = pe_info.cost_pct
                    forward_pe[pos] = fp
            if idx in raw_signal_day.index:
                raw_signal_rows[pos] = raw_signal_day.loc[idx].to_dict()

    premium_paths_ce, path_lens_ce = pad_premium_paths(forward_ce, max_bars)
    premium_paths_pe, path_lens_pe = pad_premium_paths(forward_pe, max_bars)

    valid_ce = ~np.isnan(entry_premium_ce)
    valid_pe = ~np.isnan(entry_premium_pe)
    n_dropped_both_illiquid = int((~valid_ce & ~valid_pe).sum())

    comp_df = pd.DataFrame({
        "fut_return_5m": pd.to_numeric(entries.get("fut_return_5m"), errors="coerce"),
        "fut_return_15m": pd.to_numeric(entries.get("fut_return_15m"), errors="coerce"),
        "vix_intraday_chg": pd.to_numeric(entries.get("vix_intraday_chg"), errors="coerce"),
        "fut_close": pd.to_numeric(entries.get("fut_close"), errors="coerce"),
    })
    for col in COMPOSITE_RAW_COLUMNS:
        comp_df[col] = [
            (raw_signal_rows[i].get(col) if raw_signal_rows[i] is not None else None) for i in range(n)
        ]
    comp_votes = composite_vote(comp_df).to_numpy()
    comp_coverage = float(pd.Series(comp_votes).notna().mean())

    result.update({
        "status": "ok",
        "n_dropped_both_illiquid": n_dropped_both_illiquid,
        "n_no_snapshot_day": n_no_snapshot_day,
        "n_bar_not_found": n_bar_not_found,
        "n_valid_ce": int(valid_ce.sum()),
        "n_valid_pe": int(valid_pe.sum()),
        "composite_coverage": round(comp_coverage, 4),
        "_trade_date": entries["trade_date"].astype(str).to_numpy(),
        "_entry_premium_ce": entry_premium_ce, "_entry_premium_pe": entry_premium_pe,
        "_premium_paths_ce": premium_paths_ce, "_premium_paths_pe": premium_paths_pe,
        "_path_lens_ce": path_lens_ce, "_path_lens_pe": path_lens_pe,
        "_cost_pct_ce": cost_pct_ce, "_cost_pct_pe": cost_pct_pe,
        "_comp_votes": comp_votes,
    })
    return result


# ── Grid evaluation for one window ──────────────────────────────────────────

def evaluate_window_grid(
    w: Dict[str, Any], grid: Sequence[ExitGridParams], *, seed: int,
) -> Dict[str, Any]:
    """Run the full exit grid on BOTH sides once, then apply the coin-flip
    (this window's own seed) and composite-vote (with coin-flip fallback)
    assignments -- returns per-combo summary stats for both, plus the
    coin-flip-selected raw inputs (for the whipsaw diagnostic) and the
    boolean side assignment itself (for seed-sensitivity re-runs)."""
    n = len(w["_entry_premium_ce"])
    outcome_ce = simulate_exit_grid_batch(
        w["_entry_premium_ce"], w["_premium_paths_ce"], w["_path_lens_ce"], w["_cost_pct_ce"], grid,
    )
    outcome_pe = simulate_exit_grid_batch(
        w["_entry_premium_pe"], w["_premium_paths_pe"], w["_path_lens_pe"], w["_cost_pct_pe"], grid,
    )
    is_ce_coinflip = assign_random_sides(n, seed=seed)
    selected_coinflip = select_outcome_by_side(is_ce_coinflip, outcome_ce, outcome_pe)

    comp_votes = w["_comp_votes"]
    is_ce_composite = np.where(comp_votes == "CE", True, np.where(comp_votes == "PE", False, is_ce_coinflip))
    selected_composite = select_outcome_by_side(is_ce_composite, outcome_ce, outcome_pe)

    coinflip_stats = [summarize_returns(selected_coinflip.net_return_pct[:, k]) for k in range(len(grid))]
    composite_stats = [summarize_returns(selected_composite.net_return_pct[:, k]) for k in range(len(grid))]

    sel_entry, sel_path, sel_lens, sel_cost = select_inputs_by_side(
        is_ce_coinflip,
        w["_entry_premium_ce"], w["_premium_paths_ce"], w["_path_lens_ce"], w["_cost_pct_ce"],
        w["_entry_premium_pe"], w["_premium_paths_pe"], w["_path_lens_pe"], w["_cost_pct_pe"],
    )
    return {
        "seed": seed,
        "coinflip_stats": coinflip_stats,
        "composite_stats": composite_stats,
        "_is_ce_coinflip": is_ce_coinflip,
        "_is_ce_composite": is_ce_composite,
        "_outcome_ce": outcome_ce, "_outcome_pe": outcome_pe,
        "_sel_entry": sel_entry, "_sel_path": sel_path, "_sel_lens": sel_lens, "_sel_cost": sel_cost,
    }


# ── Reporting helpers ────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            (str(k) if not isinstance(k, str) else k): _json_safe(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist())
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    return obj


def find_consistent_combos(
    ok_windows: Sequence[Dict[str, Any]],
    per_window_eval: Sequence[Dict[str, Any]],
    grid: Sequence[ExitGridParams],
    *,
    stat_key: str,
    recent_n: int,
    min_rows: int,
) -> List[Dict[str, Any]]:
    """For every grid combo, build the per-window `mean_actual`/`positive`/
    `thin_sample` rows `joint_direction_threshold.consistency_summary`
    expects, from THIS study's `expectancy`/`n` stats -- reusing that
    already-built cross-window consistency utility rather than
    reimplementing it (same "all-5 or the most-recent-N" bar every other
    direction/threshold study in this pipeline uses)."""
    rows = []
    for k, params in enumerate(grid):
        per_window_rows = []
        for ev in per_window_eval:
            stats = ev[stat_key][k]
            per_window_rows.append({
                "mean_actual": stats["expectancy"],
                "positive": bool(stats["expectancy"] is not None and stats["expectancy"] > 0),
                "thin_sample": stats["n"] < min_rows,
            })
        consistency = consistency_summary(per_window_rows, recent_n=recent_n)
        pooled_n = sum(s[stat_key][k]["n"] for s in per_window_eval)
        side_key = "_is_ce_coinflip" if stat_key == "coinflip_stats" else "_is_ce_composite"
        pooled_net = np.concatenate([
            _selected_net_returns(ev, k, side_key) for ev in per_window_eval
        ])
        pooled_stats = summarize_returns(pooled_net)
        rows.append({
            "combo": params.label, "stop_loss_pct": params.stop_loss_pct,
            "trail_activate_pct": params.trail_activate_pct, "trail_distance_pct": params.trail_distance_pct,
            "consistency": consistency, "pooled_n": pooled_n, "pooled_stats": pooled_stats,
        })
    return rows


def _selected_net_returns(ev: Dict[str, Any], k: int, side_key: str = "_is_ce_coinflip") -> np.ndarray:
    is_ce = ev[side_key]
    return select_outcome_by_side(is_ce, ev["_outcome_ce"], ev["_outcome_pe"]).net_return_pct[:, k]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=".run/training_view/training_view_nifty_option_pnl.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--min-test-rows", type=int, default=DEFAULT_MIN_TEST_ROWS)
    ap.add_argument("--entry-top-quantile", type=float, default=DEFAULT_ENTRY_TOP_QUANTILE)
    ap.add_argument("--max-bars", type=int, default=DEFAULT_MAX_BARS)
    ap.add_argument("--session-end-min", type=int, default=DEFAULT_SESSION_END_MIN)
    ap.add_argument("--stop-grid", default=",".join(str(x) for x in DEFAULT_STOP_GRID))
    ap.add_argument("--activate-grid", default=",".join(str(x) for x in DEFAULT_TRAIL_ACTIVATE_GRID))
    ap.add_argument("--distance-grid", default=",".join(str(x) for x in DEFAULT_TRAIL_DISTANCE_GRID))
    ap.add_argument("--base-seed", type=int, default=DEFAULT_BASE_SEED)
    ap.add_argument("--extra-seeds", default=",".join(str(s) for s in DEFAULT_EXTRA_SEEDS))
    ap.add_argument("--extra-seed-window", type=int, default=-1, help="1-indexed window id, or -1 for the most recent OK window")
    ap.add_argument("--recent-n", type=int, default=DEFAULT_RECENT_N)
    ap.add_argument("--min-rows-per-combo", type=int, default=DEFAULT_MIN_ROWS_PER_COMBO)
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    stop_grid = [float(x) for x in args.stop_grid.split(",") if x.strip()]
    activate_grid = [float(x) for x in args.activate_grid.split(",") if x.strip()]
    distance_grid = [float(x) for x in args.distance_grid.split(",") if x.strip()]
    extra_seeds = [int(x) for x in args.extra_seeds.split(",") if x.strip()]
    grid = build_exit_grid(stop_grid, activate_grid, distance_grid)
    log.info("exit grid: %d stop x %d activate x %d distance = %d combinations", len(stop_grid), len(activate_grid), len(distance_grid), len(grid))

    log.info("loading %s", args.training_view_csv)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "time"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates (%s..%s)", len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())

    windows = build_expanding_windows(sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days)
    log.info("built %d walk-forward windows", len(windows))

    window_records: List[Dict[str, Any]] = []
    for w in windows:
        log.info("=== window %d: train <= %s, test %s..%s ===", w.window_id, w.train_end, w.test_start, w.test_end)
        rec = build_window_entries(
            df, w, lot_size=args.lot_size, entry_top_quantile=args.entry_top_quantile,
            parquet_base=args.parquet_base, max_bars=args.max_bars, session_end_min=args.session_end_min,
            min_test_rows=args.min_test_rows,
        )
        log.info(
            "window %d status=%s n_fired=%s n_valid_ce=%s n_valid_pe=%s composite_coverage=%s",
            w.window_id, rec.get("status"), rec.get("n_fired_entries"), rec.get("n_valid_ce"),
            rec.get("n_valid_pe"), rec.get("composite_coverage"),
        )
        window_records.append(rec)

    ok_windows = [w for w in window_records if w.get("status") == "ok"]
    if not ok_windows:
        log.error("no OK windows -- cannot run the exit grid")
        report = {"windows": _json_safe(window_records), "error": "no OK windows"}
        if args.output_json:
            Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output_json).write_text(json.dumps(report, indent=2, default=str))
        return 1

    log.info("=== running exit grid (primary coin-flip seed) per window ===")
    per_window_eval: List[Dict[str, Any]] = []
    for w in ok_windows:
        seed = args.base_seed + w["window"]
        ev = evaluate_window_grid(w, grid, seed=seed)
        log.info("window %d: coin-flip seed=%d", w["window"], seed)
        per_window_eval.append(ev)

    # ── Whipsaw diagnostic (coin-flip-selected leg, per window) ────────────
    log.info("=== hard-stop whipsaw diagnostic (coin-flip-selected leg) ===")
    whipsaw_by_window = []
    for w, ev in zip(ok_windows, per_window_eval):
        rows = hard_stop_whipsaw_report(ev["_sel_entry"], ev["_sel_path"], ev["_sel_lens"], ev["_sel_cost"], stop_grid)
        whipsaw_by_window.append({"window": w["window"], "rows": rows})
        for r in rows:
            log.info("  window %d stop=%.2f: n_stopped=%d whipsaw_rate=%s", w["window"], r["stop_loss_pct"], r["n_stopped"], r["whipsaw_rate"])

    # ── Cross-window consistency per combo (coin-flip primary) ─────────────
    log.info("=== cross-window consistency, coin-flip (primary seed) ===")
    coinflip_combo_rows = find_consistent_combos(
        ok_windows, per_window_eval, grid, stat_key="coinflip_stats", recent_n=args.recent_n, min_rows=args.min_rows_per_combo,
    )
    consistent_coinflip = [r for r in coinflip_combo_rows if r["consistency"]["all_windows_positive"] or r["consistency"]["recent_all_positive"]]
    log.info("%d / %d combos consistently positive (all-%d or most-recent-%d) under coin-flip", len(consistent_coinflip), len(grid), len(ok_windows), args.recent_n)
    best_pooled = max(coinflip_combo_rows, key=lambda r: (r["pooled_stats"]["expectancy"] if r["pooled_stats"]["expectancy"] is not None else -999.0))

    # ── Composite-vote comparison (same grid, same fired bars) ──────────────
    log.info("=== cross-window consistency, composite-vote direction ===")
    composite_combo_rows = find_consistent_combos(
        ok_windows, per_window_eval, grid, stat_key="composite_stats", recent_n=args.recent_n, min_rows=args.min_rows_per_combo,
    )

    # ── Seed-sensitivity check on one designated window ─────────────────────
    target_window_idx = len(ok_windows) - 1 if args.extra_seed_window == -1 else next(
        (i for i, w in enumerate(ok_windows) if w["window"] == args.extra_seed_window), len(ok_windows) - 1,
    )
    target_w = ok_windows[target_window_idx]
    primary_seed = args.base_seed + target_w["window"]
    seed_sensitivity: Dict[str, Any] = {"window": target_w["window"], "seeds_tried": [primary_seed] + extra_seeds, "per_seed_best_combo_expectancy": {}}
    log.info("=== seed-sensitivity check on window %d (seeds=%s) ===", target_w["window"], seed_sensitivity["seeds_tried"])
    best_k = grid.index(next(g for g in grid if g.label == best_pooled["combo"]))
    for seed in seed_sensitivity["seeds_tried"]:
        ev = evaluate_window_grid(target_w, grid, seed=seed)
        stats = ev["coinflip_stats"][best_k]
        seed_sensitivity["per_seed_best_combo_expectancy"][seed] = stats
        log.info("  seed=%d best-pooled-combo(%s) on window %d: expectancy=%s win_rate=%s n=%s", seed, best_pooled["combo"], target_w["window"], stats["expectancy"], stats["win_rate"], stats["n"])

    report = {
        "config": {
            "training_view_csv": args.training_view_csv, "lot_size": args.lot_size,
            "n_windows": args.n_windows, "min_train_days": args.min_train_days,
            "entry_top_quantile": args.entry_top_quantile, "max_bars": args.max_bars,
            "session_end_min": args.session_end_min, "stop_grid": stop_grid,
            "activate_grid": activate_grid, "distance_grid": distance_grid,
            "n_grid_combos": len(grid), "base_seed": args.base_seed,
        },
        "windows": _json_safe(window_records),
        "coinflip_combo_grid": _json_safe(coinflip_combo_rows),
        "coinflip_consistent_combos": _json_safe(consistent_coinflip),
        "coinflip_best_pooled_combo": _json_safe(best_pooled),
        "composite_combo_grid": _json_safe(composite_combo_rows),
        "whipsaw_by_window": _json_safe(whipsaw_by_window),
        "seed_sensitivity": _json_safe(seed_sensitivity),
    }

    print("\n" + "=" * 78)
    print("=== COIN-FLIP DIRECTION: is ANY stop/trailing combo consistently positive? ===")
    print("=" * 78)
    for r in sorted(coinflip_combo_rows, key=lambda r: -(r["pooled_stats"]["expectancy"] or -999)):
        c = r["consistency"]
        print(f"{r['combo']:<32} pooled_n={r['pooled_stats']['n']:<6} pooled_exp={r['pooled_stats']['expectancy']:<9} "
              f"win_rate={r['pooled_stats']['win_rate']:<7} mean_win={r['pooled_stats']['mean_win']:<9} mean_loss={r['pooled_stats']['mean_loss']:<9} "
              f"means_by_window={c['means_by_window']} all_pos={c['all_windows_positive']} recent{args.recent_n}_pos={c['recent_all_positive']}")
    print(f"\n-> {len(consistent_coinflip)} / {len(grid)} combos consistently positive under coin-flip direction")
    print(f"-> best pooled-expectancy combo: {best_pooled['combo']} (pooled expectancy={best_pooled['pooled_stats']['expectancy']}, n={best_pooled['pooled_stats']['n']})")

    print("\n" + "=" * 78)
    print("=== HARD-STOP WHIPSAW DIAGNOSTIC (coin-flip-selected leg) ===")
    print("=" * 78)
    for wr in whipsaw_by_window:
        print(f"window {wr['window']}: " + ", ".join(f"stop={r['stop_loss_pct']:.2f}->whipsaw={r['whipsaw_rate']}(n_stopped={r['n_stopped']})" for r in wr["rows"]))

    print("\n" + "=" * 78)
    print(f"=== SEED-SENSITIVITY on window {target_w['window']}, best combo {best_pooled['combo']} ===")
    print("=" * 78)
    for seed, stats in seed_sensitivity["per_seed_best_combo_expectancy"].items():
        print(f"  seed={seed}: expectancy={stats['expectancy']} win_rate={stats['win_rate']} n={stats['n']}")

    print("\n" + "=" * 78)
    print("=== COMPOSITE-VOTE DIRECTION (secondary test, same grid/fired-bars) ===")
    print("=" * 78)
    consistent_composite = [r for r in composite_combo_rows if r["consistency"]["all_windows_positive"] or r["consistency"]["recent_all_positive"]]
    print(f"-> {len(consistent_composite)} / {len(grid)} combos consistently positive under composite-vote direction")
    for r in sorted(composite_combo_rows, key=lambda r: -(r["pooled_stats"]["expectancy"] or -999))[:10]:
        print(f"{r['combo']:<32} pooled_exp={r['pooled_stats']['expectancy']} win_rate={r['pooled_stats']['win_rate']} n={r['pooled_stats']['n']}")
    print("\ncomposite coverage by window: " + ", ".join(f"w{w['window']}={w.get('composite_coverage')}" for w in ok_windows))

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
