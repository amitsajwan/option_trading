"""Big-move-agreement + fade-the-breakout direction study (NIFTY, 2026-09-27).

Third of three direction-research tracks run today (see
`ml_pipeline_2.pipeline.breakout_agreement_direction`'s module docstring for
the full background and the two completed sibling tracks, both clean
negatives). Answers: does EITHER of this project's two historically-cited
directional edges -- "vwap + PCR agreement on big moves (~150-160pt on
BankNifty) -> ~59-62%" (`DIRECTION_TREE_FINDINGS.md`) or "fade the opening-
range breakout -> ~59% (continuation ~41.3%)" (`BMM_RESULTS.md`,
`COMPRESSION_STATE_ENGINE_PLAN.md`) -- survive (a) real walk-forward
validation with bootstrap/permutation statistics (both historical findings
were thin, unvalidated, search-selected samples of 83-181 bars) and (b) the
delayed-action ("foresight is priced in") stress test that already sank a
trade-tested version of the big-move claim
(`MODEL_REBUILD_SPEC_2026-07-11.md` Amendment 4)?

This is registry-recorded research only. It must NEVER import
`strategy_app.ml.entry_direction_resolver`,
`strategy_app.engines.strategies.ml_entry`, `strategy_app.brain
.regime_director`, or `strategy_app.market.regime` -- see
`breakout_agreement_direction.py`'s module docstring for why (both signal
variants are hand-transcribed from reading, not importing, that logic).

Methodology (matches the two completed sibling tracks' rigor and window
structure EXACTLY, for direct comparability):
  1. Split the full NIFTY history into 5 expanding-origin walk-forward
     windows via `conditional_direction.build_expanding_windows`, SAME
     `min_train_days=200` the sibling tracks used on this identical input
     file -- produces the identical window boundaries (test windows
     2025-08-26..2026-07-31).
  2. Per window, per signal config (variant A at each of 5 move-size
     thresholds; variant B fade/continuation, confirmed/unconfirmed): fire
     the rule on the window's TEST bars only (no fitting -- these are
     hand-specified rules, not learned models, so TRAIN is not used at all)
     and score fire rate + accuracy against `payoff_winning_side`, with a
     bootstrap CI and a Monte-Carlo permutation p-value against a flat 50%
     baseline (`breakout_agreement_direction.evaluate_signal_accuracy`).
  3. Report every window's numbers individually, plus a pooled (weighted by
     real counts, not average-of-averages) table across all OK windows.
  4. Report whether variant A and variant B agree or diverge on bars where
     BOTH fire (`breakout_agreement_direction.variant_agreement_report`).
  5. Decisive test: for every config whose POOLED accuracy clears
     `--stress-test-edge-threshold` (default 0.55), re-score the SAME fired
     bars' outcome using entry premiums sampled 1 and 2 bars AFTER the
     original fire, holding the SAME original target exit bar (mirrors
     `run_repricing_wall_control.py`'s exact design, applied to one specific
     direction call's payoff instead of an entry-timing model's top
     decile): both (a) whether the FROZEN vote's win/loss verdict flips
     (`conditional_direction.check_direction_repricing_wall`) and (b) the
     mean net (post-cost) return of the SPECIFIC called leg at each delay
     (`validation.check_repricing_wall`, reused verbatim -- the same
     function `run_repricing_wall_control.py` itself calls). Gentle decay
     across delays (like the entry-timing model's own validated 36.3% ->
     34.3% -> 32.6%) supports a real, tradeable pattern; collapse or
     inversion matches this project's three prior documented straddle/
     vertical failures and this exact big-move claim's own prior trade-test
     failure.

Usage:
    python -m ml_pipeline_2.scripts.study_breakout_agreement_direction \\
        --input-csv .run/training_view/training_view_nifty_direction_condition.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 \\
        --output-json .run/breakout_agreement/nifty_breakout_agreement_direction_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("study_breakout_agreement_direction")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.breakout_agreement_direction import (  # noqa: E402
    BIG_MOVE_PCT_THRESHOLDS,
    big_move_agreement_vote,
    breakout_continuation_vote,
    breakout_direction_vote,
    evaluate_signal_accuracy,
    fade_breakout_vote,
    leg_return_for_side,
    variant_agreement_report,
)
from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
    check_direction_repricing_wall,
)
from ml_pipeline_2.pipeline.entry_quality_direction import compute_payoff_at_delay  # noqa: E402
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_EDGE_THRESHOLD = 0.55
DEFAULT_MIN_ROWS = 30

# Config name -> (vote_fn, kwargs). Variant A swept per move threshold below;
# variant B has 4 fixed configs (fade/continuation x confirmed/unconfirmed).
VARIANT_B_CONFIGS: Dict[str, Dict[str, Any]] = {
    "breakout_fade_confirmed": {"fn": fade_breakout_vote, "kwargs": {"require_confirmation": True}},
    "breakout_fade_unconfirmed": {"fn": fade_breakout_vote, "kwargs": {"require_confirmation": False}},
    "breakout_continuation_confirmed": {"fn": breakout_continuation_vote, "kwargs": {"require_confirmation": True}},
    "breakout_continuation_unconfirmed": {"fn": breakout_continuation_vote, "kwargs": {"require_confirmation": False}},
}


# ── Window <-> DataFrame plumbing ────────────────────────────────────────────

def select_test_frame(df: pd.DataFrame, window: WalkForwardWindow) -> pd.DataFrame:
    return df[(df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)].reset_index(drop=True)


def build_all_configs(move_thresholds: Sequence[float]) -> Dict[str, Dict[str, Any]]:
    configs: Dict[str, Dict[str, Any]] = {}
    for thr in move_thresholds:
        configs[f"big_move_agreement_thr{thr}"] = {
            "fn": big_move_agreement_vote, "kwargs": {"move_threshold": thr},
        }
    configs.update(VARIANT_B_CONFIGS)
    return configs


# ── Per-window evaluation ────────────────────────────────────────────────────

def evaluate_window(
    df: pd.DataFrame,
    window: WalkForwardWindow,
    configs: Dict[str, Dict[str, Any]],
    *,
    min_rows: int = DEFAULT_MIN_ROWS,
) -> Dict[str, Any]:
    test_df = select_test_frame(df, window)
    result: Dict[str, Any] = {
        "window": window.window_id,
        "test_start": window.test_start, "test_end": window.test_end,
        "n_test": len(test_df),
        "signals": {},
    }
    if len(test_df) < min_rows:
        result["status"] = "thin_sample"
        return result
    result["status"] = "ok"
    truth = test_df["payoff_winning_side"]
    votes_by_config: Dict[str, pd.Series] = {}
    for name, cfg in configs.items():
        votes = cfg["fn"](test_df, **cfg["kwargs"])
        votes_by_config[name] = votes
        result["signals"][name] = evaluate_signal_accuracy(votes, truth)
    result["_test_df"] = test_df
    result["_votes"] = votes_by_config
    return result


# ── Pooling across windows ──────────────────────────────────────────────────

def pool_signal_stats(window_results: Sequence[Dict[str, Any]], configs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Pool each signal's fired-bar correctness across every OK window
    (weighted by real counts, then re-run the bootstrap/permutation stats
    once on the FULL pooled correctness vector -- not an average of
    per-window p-values, which would not be statistically meaningful)."""
    ok_windows = [w for w in window_results if w.get("status") == "ok"]
    pooled: Dict[str, Any] = {}
    for name in configs:
        all_votes = []
        all_truth = []
        for w in ok_windows:
            all_votes.append(w["_votes"][name])
            all_truth.append(w["_test_df"]["payoff_winning_side"])
        votes = pd.concat(all_votes, ignore_index=True) if all_votes else pd.Series([], dtype=object)
        truth = pd.concat(all_truth, ignore_index=True) if all_truth else pd.Series([], dtype=object)
        pooled[name] = evaluate_signal_accuracy(votes, truth)
    return pooled


# ── Delayed-action stress test ──────────────────────────────────────────────

def recompute_called_leg_at_delays(
    rows: pd.DataFrame,
    votes: Sequence[Optional[str]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
) -> Dict[int, Dict[str, list]]:
    """For every fired row (`trade_date` + `bar_index`) with its frozen
    `votes` call, recompute at entry delay 0 (the original fire bar -- same
    convention `payoff_winning_side` itself was labeled with), 1, and 2 bars
    later, holding the SAME original target exit bar throughout
    (`entry_quality_direction.compute_payoff_at_delay`'s exact re-pricing
    convention -- the same one `validation.check_repricing_wall` /
    `run_repricing_wall_control.py` already use elsewhere in this pipeline):
    (a) `winning_side` at that delay and (b) the CALLED leg's own net
    (post-cost) return at that delay
    (`breakout_agreement_direction.leg_return_for_side` -- NOT
    `payoff_score`'s direction-agnostic best-of-two). Returns
    `{0: {"winning_side": [...], "called_return": [...]}, 1: {...}, 2:
    {...}}`, one entry per input row in the SAME order; None where
    unresolvable (missing snapshot day, delayed entry or the fixed exit
    falls outside the session) -- dropped, never imputed, by callers."""
    rows = rows.reset_index(drop=True)
    votes = list(votes)
    n = len(rows)
    out: Dict[int, Dict[str, list]] = {
        d: {"winning_side": [None] * n, "called_return": [None] * n} for d in (0, 1, 2)
    }
    by_date: Dict[str, List[int]] = {}
    for i, d in enumerate(rows["trade_date"].astype(str)):
        by_date.setdefault(d, []).append(i)

    for trade_date, idxs in by_date.items():
        snaps = read_day_snapshots(parquet_base, trade_date)
        if not snaps:
            continue
        for i in idxs:
            bar_index = int(rows.loc[i, "bar_index"])
            side = votes[i]
            for delay in (0, 1, 2):
                bp = compute_payoff_at_delay(
                    snaps, bar_index, horizon_bars=horizon_bars, delay_bars=delay, lot_size=lot_size,
                )
                if bp is None:
                    continue
                out[delay]["winning_side"][i] = bp.winning_side
                out[delay]["called_return"][i] = leg_return_for_side(bp, side)
    return out


def run_delayed_stress_test(
    fired_rows: pd.DataFrame,
    votes: Sequence[Optional[str]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    delays = recompute_called_leg_at_delays(
        fired_rows, votes, parquet_base, horizon_bars=horizon_bars, lot_size=lot_size,
    )
    direction_report = check_direction_repricing_wall(
        votes_at_fire=list(votes), truth_at_fire=delays[0]["winning_side"],
        votes_delay_1=list(votes), truth_delay_1=delays[1]["winning_side"],
        votes_delay_2=list(votes), truth_delay_2=delays[2]["winning_side"],
        min_paired_bars=min_paired_bars, edge_threshold=edge_threshold, raise_on_fail=False,
    )
    payoff_report = check_repricing_wall(
        payoff_at_fire=delays[0]["called_return"], payoff_delay_1=delays[1]["called_return"],
        payoff_delay_2=delays[2]["called_return"], min_paired_bars=min_paired_bars, raise_on_fail=False,
    )
    return {
        "direction_flip_test": asdict(direction_report),
        "called_leg_payoff_test": asdict(payoff_report),
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-csv", default=".run/training_view/training_view_nifty_direction_condition.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data", help="Needed only for the delayed-action stress test")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--move-thresholds", default=",".join(str(t) for t in BIG_MOVE_PCT_THRESHOLDS))
    ap.add_argument("--stress-test-edge-threshold", type=float, default=DEFAULT_EDGE_THRESHOLD)
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    move_thresholds = tuple(float(t) for t in args.move_thresholds.split(","))
    configs = build_all_configs(move_thresholds)

    log.info("loading %s", args.input_csv)
    df = pd.read_csv(args.input_csv)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates (%s..%s)", len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())

    windows = build_expanding_windows(
        sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days,
    )
    log.info("built %d walk-forward windows", len(windows))

    window_results = [evaluate_window(df, w, configs) for w in windows]
    pooled = pool_signal_stats(window_results, configs)

    # Agreement/divergence between the two variant families, pooled across
    # all OK windows -- default representative configs: the MIDDLE move
    # threshold for variant A, confirmed fade for variant B.
    ok_windows = [w for w in window_results if w.get("status") == "ok"]
    default_a_name = f"big_move_agreement_thr{sorted(move_thresholds)[len(move_thresholds) // 2]}"
    default_b_name = "breakout_fade_confirmed"
    agreement_report: Dict[str, Any] = {"status": "skipped_no_ok_windows"}
    if ok_windows:
        pooled_votes_a = pd.concat([w["_votes"][default_a_name] for w in ok_windows], ignore_index=True)
        pooled_votes_b = pd.concat([w["_votes"][default_b_name] for w in ok_windows], ignore_index=True)
        pooled_truth = pd.concat([w["_test_df"]["payoff_winning_side"] for w in ok_windows], ignore_index=True)
        agreement_report = variant_agreement_report(pooled_votes_a, pooled_votes_b, pooled_truth)
        agreement_report["variant_a_config"] = default_a_name
        agreement_report["variant_b_config"] = default_b_name

    stress_results: Dict[str, Any] = {}
    if not args.skip_stress_test and ok_windows:
        for name in configs:
            pooled_stats = pooled.get(name, {})
            acc = pooled_stats.get("accuracy")
            if acc is None or acc < args.stress_test_edge_threshold:
                continue
            fired_frames = []
            fired_votes: List[Optional[str]] = []
            for w in ok_windows:
                v = w["_votes"][name]
                mask = v.notna()
                fired_frames.append(w["_test_df"].loc[mask, ["trade_date", "bar_index"]])
                fired_votes.extend(v[mask].tolist())
            fired_rows = pd.concat(fired_frames, ignore_index=True) if fired_frames else pd.DataFrame()
            if len(fired_rows) == 0:
                continue
            log.info("running delayed-action stress test for %s (n_fired=%d, pooled accuracy=%.4f)", name, len(fired_rows), acc)
            stress_results[name] = run_delayed_stress_test(
                fired_rows, fired_votes, args.parquet_base,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size,
                edge_threshold=args.stress_test_edge_threshold,
            )

    report = {
        "n_windows": len(windows),
        "move_thresholds": list(move_thresholds),
        "windows": _json_safe([{k: v for k, v in w.items() if not k.startswith("_")} for w in window_results]),
        "pooled_signal_stats": _json_safe(pooled),
        "variant_agreement": _json_safe(agreement_report),
        "stress_test": _json_safe(stress_results),
    }

    print("\n=== Big-move-agreement / fade-the-breakout direction study: per-window results ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: test {w['test_start']}..{w['test_end']} (n={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')}")
            continue
        for name, stats in w["signals"].items():
            if stats["n_voted"] == 0:
                print(f"  {name:38s} n_voted=0 (no fires this window)")
                continue
            print(
                f"  {name:38s} coverage={stats['coverage']:.4f} n_voted={stats['n_voted']:>6d} "
                f"accuracy={stats['accuracy']} 95%CI=[{stats['bootstrap_ci_low']}, {stats['bootstrap_ci_high']}] "
                f"perm_p={stats['permutation_p_value']}"
            )

    print("\n=== Pooled across all OK windows ===")
    for name, stats in pooled.items():
        if stats["n_voted"] == 0:
            print(f"  {name:38s} n_voted=0")
            continue
        print(
            f"  {name:38s} coverage={stats['coverage']:.4f} n_voted={stats['n_voted']:>6d} "
            f"accuracy={stats['accuracy']} 95%CI=[{stats['bootstrap_ci_low']}, {stats['bootstrap_ci_high']}] "
            f"perm_p={stats['permutation_p_value']}"
        )

    print(f"\n=== Variant agreement ({default_a_name} vs {default_b_name}) ===")
    print(f"  {agreement_report}")

    if stress_results:
        print("\n=== Delayed-action stress test (mirrors run_repricing_wall_control.py) ===")
        for name, r in stress_results.items():
            df_r = r["direction_flip_test"]
            pf_r = r["called_leg_payoff_test"]
            print(f"  {name}:")
            print(
                f"    direction: ok={df_r['ok']} n={df_r['n_paired_bars']} "
                f"acc_fire={df_r.get('accuracy_at_fire')} acc_d1={df_r.get('accuracy_delay_1')} "
                f"acc_d2={df_r.get('accuracy_delay_2')} -- {df_r.get('reason')}"
            )
            print(
                f"    payoff:    ok={pf_r['ok']} n={pf_r['n_paired_bars']} "
                f"mean_fire={pf_r.get('mean_payoff_at_fire')} mean_d1={pf_r.get('mean_payoff_delay_1')} "
                f"mean_d2={pf_r.get('mean_payoff_delay_2')} p_d1={pf_r.get('wilcoxon_p_delay_1')} "
                f"p_d2={pf_r.get('wilcoxon_p_delay_2')} -- {pf_r.get('reason')}"
            )
    else:
        print("\n=== Delayed-action stress test ===\n  no config cleared the edge threshold -- nothing to stress-test")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
