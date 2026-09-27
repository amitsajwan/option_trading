"""Entry-quality-conditioned direction study (NIFTY, 2026-09-27).

Answers a narrower question than this project's six prior, exhausted
unconditional direction-prediction attempts (per-member signal accuracy
48-52%, a 31-combination ablation grid, a 2026 OOS quorum inversion to
43.9%, order-flow proxies at ~50%, a 50/50 split on high-ML-probability
bars, and 125 real live trades with zero net-positive days -- see
`ml_pipeline_2.pipeline.conditional_direction`'s module docstring for the
full citation trail): **is CE-vs-PE direction more callable specifically on
the bars an INDEPENDENT entry-timing model
(`ml_pipeline_2.scripts.train_entry_option_payoff_v1`, itself
direction-agnostic) rates as high quality?**

This is registry-recorded research only. It must NEVER import
`strategy_app.ml.entry_direction_resolver`,
`strategy_app.engines.strategies.ml_entry`, or touch any `.env.compose` /
`docker-compose*.yml` / deployed model bundle / `verified_candidates.py` --
see `ml_pipeline_2.pipeline.conditional_direction`'s and
`ml_pipeline_2.pipeline.entry_quality_direction`'s module docstrings for why
(both are import-isolated from the resolver by construction, not by
convention alone).

Methodology (mandatory rigor per the task this was built against -- a
single train/holdout split is NOT trusted in this project; see
`docs/archive/handovers_status/PROJECT_PLAN.md`'s ATM_PE_15 precedent):

  1. Split the full available NIFTY history (sorted trade_dates) into a
     seed training block plus N sequential, EXPANDING-origin walk-forward
     windows (`conditional_direction.build_expanding_windows`): window i's
     test set is a contiguous later chunk of dates; its train set is every
     date strictly before that chunk. No date is ever in both a window's
     train and test set, and payoff labels never cross a trade_date
     boundary (`option_payoff_labels.label_day`), so no extra embargo gap
     is needed.
  2. Per window: fit the entry-payoff regressor (same architecture as
     `train_entry_option_payoff_v1.py`) on TRAIN only, score TEST
     out-of-sample, and fit TRAIN's own out-of-fold entry-payoff scores
     (`entry_quality_direction.fit_entry_payoff_oof_scores`) so TRAIN rows
     can also be decile-bucketed without any row seeing its own label.
  3. Per window: bucket TEST bars into equal-count deciles by predicted
     entry-payoff score, and compute every existing rule-based direction
     signal's (+ the reduced composite's) accuracy against the REAL
     `payoff_winning_side` ground truth, PER DECILE
     (`conditional_direction.signal_accuracy_by_decile`).
  4. Per window: fit a fresh XGBoost direction classifier (CE=1/PE=0) on
     ALL TRAIN bars ("unconditioned") and a second one restricted to TRAIN's
     own top-1-2-decile bars ("conditioned"), then compare both models'
     accuracy on the SAME TEST top-1-2-decile bars with a paired bootstrap
     CI + permutation test on the difference
     (`conditional_direction.paired_accuracy_comparison`).
  5. Report every window's numbers individually (not just a pooled
     average), though a pooled table is also reported for convenience.
  6. If pooled top-decile accuracy for the composite vote, the single best
     individual signal, or the conditioned classifier clears
     `--stress-test-edge-threshold` (default 0.55), re-score the SAME fired
     bars' ground truth using option premiums sampled 1 and 2 bars after
     the original fire (frozen vote/prediction, delayed truth only -- exact
     mirror of `validation.check_repricing_wall`'s re-pricing pattern) via
     `conditional_direction.check_direction_repricing_wall`. A signal whose
     accuracy collapses to/below chance once delayed is very likely
     measuring the option chain's own repricing of the same information at
     the instant of peak confidence, not a structural, tradeable edge --
     the exact failure mode that sank three prior documented entry-timing
     experiments in this project.

Usage:
    python -m ml_pipeline_2.scripts.study_entry_quality_conditioned_direction \\
        --input-csv .run/training_view/training_view_nifty_direction_condition.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 \\
        --output-json .run/direction_condition/nifty_entry_quality_direction_report.json
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
log = logging.getLogger("study_entry_quality_conditioned_direction")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    SIGNAL_VOTERS,
    WalkForwardWindow,
    assign_predicted_deciles,
    build_expanding_windows,
    check_direction_repricing_wall,
    composite_vote,
    paired_accuracy_comparison,
    signal_accuracy_by_decile,
)
from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    compute_payoff_at_delay,
    encode_side_binary,
    fit_direction_classifier,
    fit_entry_payoff_model,
    fit_entry_payoff_oof_scores,
    predict_direction_side,
    score_entry_payoff,
)
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_TOP_DECILES = (9, 10)
MIN_ROWS_TO_FIT_CLASSIFIER = 30


# ── Window <-> DataFrame plumbing ────────────────────────────────────────────

def select_window_frames(df: pd.DataFrame, window: WalkForwardWindow) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split `df` (must have a `trade_date` column, string-sortable
    YYYY-MM-DD) into (train_df, test_df) for one walk-forward window, by
    plain date-range filtering -- `build_expanding_windows` already
    guarantees no leakage between windows, this just applies the cut."""
    train_df = df[(df["trade_date"] >= window.train_start) & (df["trade_date"] <= window.train_end)].reset_index(drop=True)
    test_df = df[(df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)].reset_index(drop=True)
    return train_df, test_df


# ── Conditioned vs unconditioned direction classifier ───────────────────────

def run_conditioned_vs_unconditioned(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: Sequence[str],
    train_deciles: np.ndarray,
    test_deciles: np.ndarray,
    *,
    top_deciles: Sequence[int] = DEFAULT_TOP_DECILES,
    min_rows: int = MIN_ROWS_TO_FIT_CLASSIFIER,
) -> Dict[str, Any]:
    """Fit an unconditioned classifier (all TRAIN bars) and a conditioned one
    (TRAIN bars in `top_deciles` only, per TRAIN's own out-of-fold entry-
    payoff deciles), evaluate both on the SAME TEST bars restricted to
    `top_deciles` (per TEST's own out-of-sample deciles), and compare with a
    paired bootstrap/permutation test. Returns a dict with both models'
    accuracy on that shared subset, the unconditioned model's accuracy on
    ALL test bars (sanity-check context -- should sit near the ~50-56%
    coin-flip this project has repeatedly found), and the raw per-bar
    correctness arrays (for pooling across windows / the delayed stress
    test) under keys prefixed `_` (dropped by JSON serialization)."""
    y_train_all = encode_side_binary(train_df["payoff_winning_side"])
    y_test_all = encode_side_binary(test_df["payoff_winning_side"])

    modelA, medA = fit_direction_classifier(train_df[list(feature_cols)], y_train_all)
    pred_all = predict_direction_side(modelA, medA, test_df[list(feature_cols)])
    acc_unconditioned_all_bars = float((pred_all == y_test_all).mean()) if len(y_test_all) else float("nan")

    top_mask_train = np.isin(train_deciles, list(top_deciles))
    top_mask_test = np.isin(test_deciles, list(top_deciles))
    n_train_top = int(top_mask_train.sum())
    n_test_top = int(top_mask_test.sum())

    result: Dict[str, Any] = {
        "n_train_top_decile": n_train_top,
        "n_test_top_decile": n_test_top,
        "acc_unconditioned_all_bars": acc_unconditioned_all_bars,
        "n_test_all_bars": int(len(y_test_all)),
    }

    if n_test_top == 0:
        result["status"] = "no_top_decile_test_bars"
        return result

    X_test_top = test_df.loc[top_mask_test, list(feature_cols)]
    y_test_top = y_test_all[top_mask_test]
    pred_a_top = predict_direction_side(modelA, medA, X_test_top)
    correct_a = (pred_a_top == y_test_top)
    result["acc_unconditioned_top_decile"] = float(correct_a.mean())

    if n_train_top < min_rows:
        result["status"] = "thin_top_decile_train_sample"
        result["_correct_unconditioned"] = correct_a
        return result

    modelB, medB = fit_direction_classifier(
        train_df.loc[top_mask_train, list(feature_cols)], y_train_all[top_mask_train],
    )
    pred_b_top = predict_direction_side(modelB, medB, X_test_top)
    correct_b = (pred_b_top == y_test_top)
    result["acc_conditioned_top_decile"] = float(correct_b.mean())

    comparison = paired_accuracy_comparison(correct_b.tolist(), correct_a.tolist())
    result["status"] = "ok"
    result["comparison_conditioned_minus_unconditioned"] = asdict(comparison)
    result["_correct_unconditioned"] = correct_a
    result["_correct_conditioned"] = correct_b
    result["_pred_unconditioned"] = pred_a_top
    result["_pred_conditioned"] = pred_b_top
    return result


# ── Delayed-action stress test ──────────────────────────────────────────────

def recompute_winning_side_at_delay(
    rows: pd.DataFrame, parquet_base: str, *, delay_bars: int, horizon_bars: int, lot_size: int,
) -> pd.Series:
    """For every row (must have `trade_date` + `bar_index` identifying its
    position in that day's raw snapshot sequence), recompute
    `payoff_winning_side` using an entry premium sampled `delay_bars` bars
    after the original fire, holding to the SAME original target exit bar.
    Returns a Series aligned to `rows.index` (None where unresolvable -- a
    plain object-dtype Series left at its default fill would hold NaN
    instead, which `check_direction_repricing_wall`'s `is not None` filter
    would NOT catch, silently treating an unresolved day as a present-but-
    wrong vote rather than dropping it)."""
    out = pd.Series([None] * len(rows), index=rows.index, dtype=object)
    for trade_date, group in rows.groupby("trade_date"):
        snaps = read_day_snapshots(parquet_base, str(trade_date))
        if not snaps:
            continue
        for idx, r in group.iterrows():
            bp = compute_payoff_at_delay(
                snaps, int(r["bar_index"]), horizon_bars=horizon_bars, delay_bars=delay_bars, lot_size=lot_size,
            )
            out.loc[idx] = bp.winning_side if bp is not None else None
    return out


def run_stress_test(
    rows: pd.DataFrame,
    votes: Sequence[Optional[str]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    edge_threshold: float = 0.55,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    """Wraps `check_direction_repricing_wall` for one (rows, votes) pair --
    `rows` must carry `trade_date`, `bar_index`, `payoff_winning_side`
    aligned 1:1 (positionally) with `votes`."""
    truth_at_fire = rows["payoff_winning_side"].tolist()
    truth_d1 = recompute_winning_side_at_delay(
        rows, parquet_base, delay_bars=1, horizon_bars=horizon_bars, lot_size=lot_size,
    ).tolist()
    truth_d2 = recompute_winning_side_at_delay(
        rows, parquet_base, delay_bars=2, horizon_bars=horizon_bars, lot_size=lot_size,
    ).tolist()
    report = check_direction_repricing_wall(
        votes_at_fire=list(votes), truth_at_fire=truth_at_fire,
        votes_delay_1=list(votes), truth_delay_1=truth_d1,
        votes_delay_2=list(votes), truth_delay_2=truth_d2,
        min_paired_bars=min_paired_bars, edge_threshold=edge_threshold, raise_on_fail=False,
    )
    return asdict(report)


# ── Per-window orchestration ─────────────────────────────────────────────────

def evaluate_window(
    df: pd.DataFrame,
    window: WalkForwardWindow,
    feature_cols: Sequence[str],
    *,
    n_deciles: int = 10,
    top_deciles: Sequence[int] = DEFAULT_TOP_DECILES,
    min_rows: int = 200,
) -> Dict[str, Any]:
    train_df, test_df = select_window_frames(df, window)
    result: Dict[str, Any] = {
        "window": window.window_id,
        "train_start": window.train_start, "train_end": window.train_end,
        "test_start": window.test_start, "test_end": window.test_end,
        "n_train": len(train_df), "n_test": len(test_df),
    }
    if len(train_df) < min_rows or len(test_df) < min_rows:
        result["status"] = "thin_sample"
        return result

    payoff_model, medians = fit_entry_payoff_model(train_df, feature_cols, "payoff_score")
    test_scores = score_entry_payoff(payoff_model, medians, test_df, feature_cols)
    train_oof = fit_entry_payoff_oof_scores(train_df, feature_cols, "payoff_score")

    test_deciles = assign_predicted_deciles(test_scores, n_deciles=n_deciles)
    train_deciles = assign_predicted_deciles(train_oof, n_deciles=n_deciles)

    decile_table = signal_accuracy_by_decile(test_df, test_deciles, truth_col="payoff_winning_side")
    result["status"] = "ok"
    result["decile_table"] = decile_table.to_dict("records")

    classifier_result = run_conditioned_vs_unconditioned(
        train_df, test_df, feature_cols, train_deciles, test_deciles, top_deciles=top_deciles,
    )
    result["classifier_comparison"] = {k: v for k, v in classifier_result.items() if not k.startswith("_")}

    # Stash what the stress test / pooling stage needs, without re-fitting.
    top_mask_test = np.isin(test_deciles, list(top_deciles))
    result["_test_top_rows"] = test_df.loc[top_mask_test, ["trade_date", "bar_index", "payoff_winning_side"]].reset_index(drop=True)
    result["_test_top_composite_votes"] = composite_vote(test_df.loc[top_mask_test].reset_index(drop=True)).tolist()
    for name, fn in SIGNAL_VOTERS.items():
        result[f"_test_top_votes_{name}"] = fn(test_df.loc[top_mask_test].reset_index(drop=True)).tolist()
    if classifier_result.get("status") == "ok":
        result["_test_top_pred_conditioned"] = classifier_result["_pred_conditioned"].tolist()
        result["_test_top_pred_unconditioned"] = classifier_result["_pred_unconditioned"].tolist()
    return result


# ── Pooling across windows ──────────────────────────────────────────────────

def pool_decile_tables(window_results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine every OK window's decile_table rows into one pooled
    (signal, decile) -> {n_voted, n_correct, coverage, accuracy} table,
    weighting by raw counts (not a naive average-of-averages)."""
    combined: Dict[Tuple[str, int], Dict[str, int]] = {}
    n_total_by_decile: Dict[int, int] = {}
    for w in window_results:
        if w.get("status") != "ok":
            continue
        for row in w["decile_table"]:
            key = (row["signal"], row["decile"])
            acc = combined.setdefault(key, {"n_total": 0, "n_voted": 0, "n_correct": 0})
            acc["n_total"] += row["n_total"]
            acc["n_voted"] += row["n_voted"]
            acc["n_correct"] += row["n_correct"]
    out = []
    for (signal, decile), acc in sorted(combined.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        coverage = acc["n_voted"] / acc["n_total"] if acc["n_total"] else 0.0
        accuracy = acc["n_correct"] / acc["n_voted"] if acc["n_voted"] else None
        out.append({
            "signal": signal, "decile": decile,
            "n_total": acc["n_total"], "n_voted": acc["n_voted"], "n_correct": acc["n_correct"],
            "coverage": round(coverage, 4), "accuracy": round(accuracy, 4) if accuracy is not None else None,
        })
    return out


def best_signal_in_top_deciles(pooled_table: Sequence[Dict[str, Any]], top_deciles: Sequence[int], *, min_voted: int = 30) -> Optional[str]:
    """Name of the signal (excluding `composite_proxy`) with the highest
    pooled accuracy across `top_deciles`, among signals with at least
    `min_voted` votes there -- None if no signal clears that bar."""
    agg: Dict[str, Dict[str, int]] = {}
    for row in pooled_table:
        if row["decile"] not in top_deciles or row["signal"] == "composite_proxy":
            continue
        a = agg.setdefault(row["signal"], {"n_voted": 0, "n_correct": 0})
        a["n_voted"] += row["n_voted"]
        a["n_correct"] += row["n_correct"]
    best_name, best_acc = None, -1.0
    for name, a in agg.items():
        if a["n_voted"] < min_voted:
            continue
        acc = a["n_correct"] / a["n_voted"]
        if acc > best_acc:
            best_name, best_acc = name, acc
    return best_name


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
    ap.add_argument("--input-csv", required=True, help="Output of build_entry_quality_direction_dataset.py")
    ap.add_argument("--parquet-base", required=True, help="Needed only for the delayed-action stress test")
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--n-deciles", type=int, default=10)
    ap.add_argument("--top-deciles", default=",".join(str(d) for d in DEFAULT_TOP_DECILES))
    ap.add_argument("--stress-test-edge-threshold", type=float, default=0.55)
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--features", default="", help="Comma-separated feature override (empty=ENTRY_FEATURES_V3)")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    top_deciles = tuple(int(d) for d in args.top_deciles.split(","))
    feature_cols = [f.strip() for f in args.features.split(",") if f.strip()] or list(ENTRY_FEATURES_V3)

    log.info("loading %s", args.input_csv)
    df = pd.read_csv(args.input_csv)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates", len(df), df["trade_date"].nunique())

    windows = build_expanding_windows(
        sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days,
    )
    log.info("built %d walk-forward windows", len(windows))

    window_results = [
        evaluate_window(df, w, feature_cols, n_deciles=args.n_deciles, top_deciles=top_deciles)
        for w in windows
    ]

    pooled_table = pool_decile_tables(window_results)
    best_signal = best_signal_in_top_deciles(pooled_table, top_deciles)

    stress_results: Dict[str, Any] = {}
    if not args.skip_stress_test:
        ok_windows = [w for w in window_results if w.get("status") == "ok"]
        pooled_rows = pd.concat([w["_test_top_rows"] for w in ok_windows], ignore_index=True) if ok_windows else pd.DataFrame()
        if len(pooled_rows):
            composite_votes = sum((w["_test_top_composite_votes"] for w in ok_windows), [])
            stress_results["composite_proxy"] = run_stress_test(
                pooled_rows, composite_votes, args.parquet_base,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size,
                edge_threshold=args.stress_test_edge_threshold,
            )
            if best_signal:
                best_votes = sum((w[f"_test_top_votes_{best_signal}"] for w in ok_windows), [])
                stress_results[f"best_signal:{best_signal}"] = run_stress_test(
                    pooled_rows, best_votes, args.parquet_base,
                    horizon_bars=args.horizon_bars, lot_size=args.lot_size,
                    edge_threshold=args.stress_test_edge_threshold,
                )
            windows_with_conditioned = [w for w in ok_windows if "_test_top_pred_conditioned" in w]
            if windows_with_conditioned:
                cond_rows = pd.concat([w["_test_top_rows"] for w in windows_with_conditioned], ignore_index=True)
                cond_preds = sum((w["_test_top_pred_conditioned"] for w in windows_with_conditioned), [])
                cond_votes = ["CE" if p == 1 else "PE" for p in cond_preds]
                stress_results["conditioned_classifier"] = run_stress_test(
                    cond_rows, cond_votes, args.parquet_base,
                    horizon_bars=args.horizon_bars, lot_size=args.lot_size,
                    edge_threshold=args.stress_test_edge_threshold,
                )
        else:
            stress_results["skipped"] = "no OK windows with top-decile test bars"

    report = {
        "n_windows": len(windows),
        "feature_cols": feature_cols,
        "top_deciles": list(top_deciles),
        "windows": _json_safe(window_results),
        "pooled_decile_table": pooled_table,
        "best_individual_signal_in_top_deciles": best_signal,
        "stress_test": _json_safe(stress_results),
    }

    print("\n=== Entry-quality-conditioned direction study: per-window results ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: train {w['train_start']}..{w['train_end']} (n={w['n_train']}) "
              f"-> test {w['test_start']}..{w['test_end']} (n={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')}")
            continue
        cc = w["classifier_comparison"]
        print(f"  unconditioned accuracy (ALL test bars): {cc.get('acc_unconditioned_all_bars')}")
        print(f"  unconditioned accuracy (top-decile test bars, n={cc.get('n_test_top_decile')}): {cc.get('acc_unconditioned_top_decile')}")
        if cc.get("status") == "ok":
            print(f"  conditioned accuracy (top-decile test bars):   {cc.get('acc_conditioned_top_decile')}")
            cmp = cc["comparison_conditioned_minus_unconditioned"]
            print(f"  conditioned - unconditioned lift: {cmp['observed_diff']} "
                  f"95%CI=[{cmp['bootstrap_ci_low']}, {cmp['bootstrap_ci_high']}] perm_p={cmp['permutation_p_value']}")
        else:
            print(f"  classifier comparison status: {cc.get('status')}")

    print("\n=== Pooled decile table (top signal accuracy per decile, all windows combined) ===")
    for row in pooled_table:
        if row["decile"] in top_deciles or row["decile"] in (1, 5):
            print(f"  {row['signal']:>16s} decile={row['decile']:>2d} n_voted={row['n_voted']:>6d} "
                  f"coverage={row['coverage']:.3f} accuracy={row['accuracy']}")

    if stress_results:
        print("\n=== Delayed-action stress test (mirrors check_repricing_wall) ===")
        for name, r in stress_results.items():
            if not isinstance(r, dict) or "ok" not in r:
                print(f"  {name}: {r}")
                continue
            print(f"  {name}: ok={r['ok']} n_paired_bars={r['n_paired_bars']} "
                  f"acc_fire={r.get('accuracy_at_fire')} acc_d1={r.get('accuracy_delay_1')} "
                  f"acc_d2={r.get('accuracy_delay_2')} -- {r.get('reason')}")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
