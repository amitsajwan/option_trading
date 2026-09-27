"""Direction-CALL-TRUST meta-labeling study (NIFTY, 2026-09-27).

Answers: **can we predict, bar by bar, whether composite's direction call
(`strategy_app.ml.entry_direction_resolver.resolve_entry_direction_composite`)
is likely to be right or wrong THIS TIME -- using information composite
itself does not already use -- and does filtering to "meta-model says trust
this call" measurably raise composite's realized accuracy, with a real
statistical margin, consistently across independent rolling walk-forward
windows?** This is meta-labeling applied to direction-calling instead of
entry-timing -- the same pattern this codebase already shipped for entry
quality (`ml_pipeline_2.pipeline.meta_entry_features` /
`ml_pipeline_2.scripts.train_meta_entry_v1`), and it deliberately does NOT
repeat this project's exhausted unconditional-direction-accuracy question
(coin-flip, ~50-56%, confirmed at least 7 independent ways -- see
`ml_pipeline_2.pipeline.conditional_direction`'s module docstring) or the
already-closed entry-quality-CONDITIONED direction question (a clean
negative -- see `ml_pipeline_2.pipeline.entry_quality_direction`'s and
`ml_pipeline_2.scripts.study_entry_quality_conditioned_direction`'s module
docstrings). Composite's own known-dead VIX signal (units-mismatch bug,
never fires in 452 days of real history) is left as-is here, not patched --
this study evaluates composite's REAL, currently-live scoring behavior, bug
included, not a hypothetically-fixed version of it.

Data: this sandbox HAS real NIFTY history locally (contradicting an earlier,
now-superseded stalled session's leftover files, which reported it
unavailable and used FINNIFTY instead -- see
`ml_pipeline_2.pipeline.direction_trust_meta`'s module docstring for the
full account): `.run/training_view/training_view_nifty_direction_condition
.csv.gz` (built by `build_entry_quality_direction_dataset.py`, 137,285 rows,
428 trading days, 2024-11-04..2026-07-31) already carries composite's raw
scoring inputs, `payoff_winning_side`/`payoff_score` ground truth,
`SELECTED_VELOCITY_FEATURES`, and `SELECTED_REGIME_FEATURES` as plain
columns -- no fresh join or raw-parquet replay is needed for the main study,
only for the delayed-action stress test (`.data/ml_pipeline/parquet_data`,
verified present, NIFTY-confirmed via `instrument_config.py`).

Methodology (mandatory project rigor -- a single train/holdout split is not
trusted here, see `docs/archive/handovers_status/PROJECT_PLAN.md`'s
ATM_PE_15 precedent):
  1. Split the full history into 5 expanding-origin walk-forward windows via
     `conditional_direction.build_expanding_windows` with the SAME
     `min_train_days=200` the sibling entry-quality-conditioned-direction
     study used on this identical input file -- produces the identical
     window boundaries (test windows spanning 2025-08-26..2026-07-31) for
     direct comparability.
  2. Per window: restrict to composite-fired (non-abstained), ground-truth-
     labeled bars. Fit the entry-quality/payoff regressor
     (`ENTRY_FEATURES_V3`, same architecture as `train_entry_option_payoff
     _v1.py`) walk-forward-safely (out-of-fold on TRAIN, held-out on TEST)
     -- its predicted score is one of the meta-model's features (a
     genuinely different question -- "would ANY option trade clear cost
     here" -- than composite's own "which side wins", so no re-derivation
     risk; see `meta_direction_features.py`'s module docstring). Assemble
     `META_DIRECTION_FEATURE_COLUMNS` (velocity rate/acceleration features +
     regime/compression features + composite-active-streak length + the
     entry-quality score -- NONE of composite's own scoring inputs, enforced
     by `meta_direction_features.COMPOSITE_OWN_INPUT_NAMES` and a unit
     test). Fit a trust meta-model (XGBClassifier) on TRAIN predicting
     `composite_correct`, score TEST.
  3. Per window, per trust fraction (top X% of TEST bars by meta-model
     score): compare composite's baseline accuracy (all TEST-fired bars)
     against its accuracy on the trusted subset, with a paired bootstrap CI
     and a permutation-test p-value on the lift -- not just a point
     estimate.
  4. Report every window's numbers individually, not just a pooled average.
  5. Delayed-action stress test: pool all OK windows' trusted-subset bars
     (top `max(trust_fracs)`, a superset of every smaller trust fraction
     within the same window) and re-score composite's FROZEN call's
     correctness using ground truth recomputed with an entry premium
     sampled 1 and 2 bars after the original fire, holding to the same
     original exit bar (`direction_trust_meta.recompute_composite_correct_at
     _delay`) -- the exact failure mode that sank three prior documented
     entry-timing edges in this project if accuracy collapses to/below
     chance once delayed (`direction_trust_meta.summarize_delayed_trust
     _filter`, mirroring `validation.check_repricing_wall`).

Usage:
    python -m ml_pipeline_2.scripts.study_direction_trust_meta \\
        --input-csv .run/training_view/training_view_nifty_direction_condition.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 \\
        --output-json .run/direction_trust_meta/nifty_direction_trust_meta_report.json
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
log = logging.getLogger("study_direction_trust_meta")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import build_expanding_windows  # noqa: E402
from ml_pipeline_2.pipeline.direction_trust_meta import (  # noqa: E402
    DEFAULT_TRUST_FRACTIONS,
    add_bars_since_active,
    assemble_meta_feature_frame,
    compute_composite_calls,
    compute_composite_correct,
    evaluate_trust_filter,
    fit_entry_quality_score_walkforward,
    fit_trust_meta_model,
    predict_trust_score,
    recompute_composite_correct_at_delay,
    summarize_delayed_trust_filter,
    trusted_mask_per_group,
)
from ml_pipeline_2.pipeline.meta_direction_features import BARS_SINCE_ACTIVE_COL  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402

MIN_ROWS_TO_FIT = 50


# ── Per-window orchestration ─────────────────────────────────────────────────

def select_window_frames(df: pd.DataFrame, window) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_df = df[(df["trade_date"] >= window.train_start) & (df["trade_date"] <= window.train_end)].reset_index(drop=True)
    test_df = df[(df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)].reset_index(drop=True)
    return train_df, test_df


def evaluate_window(
    df: pd.DataFrame, window, *, trust_fracs: Sequence[float] = DEFAULT_TRUST_FRACTIONS,
) -> Dict[str, Any]:
    train_df, test_df = select_window_frames(df, window)
    result: Dict[str, Any] = {
        "window": window.window_id,
        "train_start": window.train_start, "train_end": window.train_end,
        "test_start": window.test_start, "test_end": window.test_end,
        "n_train": len(train_df), "n_test": len(test_df),
    }

    # Entry-quality score: fit on ALL train/test bars (not pre-filtered to
    # composite-fired) so it stays an independent estimate -- see
    # direction_trust_meta.fit_entry_quality_score_walkforward's docstring.
    train_oof, test_scores = fit_entry_quality_score_walkforward(train_df, test_df, ENTRY_FEATURES_V3)

    train_mask = train_df["composite_fired"].to_numpy() & train_df["composite_correct"].notna().to_numpy()
    test_mask = test_df["composite_fired"].to_numpy() & test_df["composite_correct"].notna().to_numpy()
    train_fired = train_df.loc[train_mask].reset_index(drop=True)
    test_fired = test_df.loc[test_mask].reset_index(drop=True)
    train_oof_fired = train_oof[train_mask]
    test_scores_fired = test_scores[test_mask]

    result["n_train_fired"] = len(train_fired)
    result["n_test_fired"] = len(test_fired)

    if len(test_fired):
        result["baseline_accuracy_test"] = float(test_fired["composite_correct"].mean())
    else:
        result["baseline_accuracy_test"] = float("nan")

    y_train = train_fired["composite_correct"].to_numpy()
    y_test = test_fired["composite_correct"].to_numpy()

    if len(train_fired) < MIN_ROWS_TO_FIT or len(test_fired) < MIN_ROWS_TO_FIT or len(np.unique(y_train)) < 2:
        result["status"] = "thin_sample_or_degenerate_train"
        return result

    X_train = assemble_meta_feature_frame(train_fired, train_oof_fired)
    X_test = assemble_meta_feature_frame(test_fired, test_scores_fired)

    model, medians = fit_trust_meta_model(X_train, y_train)
    p_test = predict_trust_score(model, medians, X_test)

    meta_auc = None
    if len(np.unique(y_test)) >= 2:
        from sklearn.metrics import roc_auc_score
        meta_auc = float(roc_auc_score(y_test, p_test))

    result["status"] = "ok"
    result["meta_auc_test"] = meta_auc
    result["trust_filters"] = [evaluate_trust_filter(y_test, p_test, f) for f in trust_fracs]

    # Stashed (not JSON-serialized -- keys prefixed `_`) for pooling / the
    # delayed-action stress test, without re-fitting anything.
    result["_test_meta"] = pd.DataFrame(
        {
            "window": window.window_id,
            "trade_date": test_fired["trade_date"].to_numpy(),
            "bar_index": test_fired["bar_index"].to_numpy(),
            "composite_direction": test_fired["composite_direction"].to_numpy(),
            "composite_correct": y_test,
            "meta_score": p_test,
        }
    )
    return result


# ── Delayed-action stress test ──────────────────────────────────────────────

def run_delayed_stress_test(
    window_results: Sequence[Dict[str, Any]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    trust_fracs: Sequence[float] = DEFAULT_TRUST_FRACTIONS,
    edge_threshold: float = 0.55,
) -> Dict[str, Any]:
    ok = [w for w in window_results if w.get("status") == "ok" and "_test_meta" in w]
    if not ok:
        return {"status": "skipped_no_ok_windows"}
    pooled = pd.concat([w["_test_meta"] for w in ok], ignore_index=True)

    max_frac = max(trust_fracs)
    union_mask = trusted_mask_per_group(pooled["window"].to_numpy(), pooled["meta_score"].to_numpy(), max_frac)
    n_union = int(union_mask.sum())
    log.info(
        "delayed stress test: recomputing ground truth at delay 1/2 for %d/%d pooled trusted bars "
        "(top %.0f%% union across %d OK windows, %d unique trade_dates)",
        n_union, len(pooled), max_frac * 100, len(ok), pooled.loc[union_mask, "trade_date"].nunique(),
    )
    subset = pooled.loc[union_mask, ["trade_date", "bar_index", "composite_direction"]]
    y1_subset = recompute_composite_correct_at_delay(
        subset, parquet_base, delay_bars=1, horizon_bars=horizon_bars, lot_size=lot_size,
    )
    y2_subset = recompute_composite_correct_at_delay(
        subset, parquet_base, delay_bars=2, horizon_bars=horizon_bars, lot_size=lot_size,
    )
    y1_full = pd.Series(np.nan, index=pooled.index)
    y2_full = pd.Series(np.nan, index=pooled.index)
    y1_full.loc[subset.index] = y1_subset
    y2_full.loc[subset.index] = y2_subset

    correct0 = pooled["composite_correct"].to_numpy()
    correct1 = y1_full.to_numpy()
    correct2 = y2_full.to_numpy()

    per_frac: Dict[str, Any] = {}
    for frac in trust_fracs:
        mask = trusted_mask_per_group(pooled["window"].to_numpy(), pooled["meta_score"].to_numpy(), frac)
        per_frac[str(frac)] = summarize_delayed_trust_filter(
            correct0, correct1, correct2, mask, edge_threshold=edge_threshold,
        )
    baseline_all = summarize_delayed_trust_filter(
        correct0, correct1, correct2, np.ones(len(pooled), dtype=bool), edge_threshold=edge_threshold,
    )
    return {"status": "ok", "n_pooled_test_fired": len(pooled), "baseline_all_bars": baseline_all, "trust_filters": per_frac}


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


def _fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.2f}%"


def print_report(window_results: List[Dict[str, Any]], stress: Dict[str, Any]) -> None:
    print("\n=== Direction-call-trust meta-labeling: per-window walk-forward results ===")
    for w in window_results:
        print(
            f"\nWindow {w['window']}: train {w['train_start']}..{w['train_end']} "
            f"(n_fired={w.get('n_train_fired')}) -> test {w['test_start']}..{w['test_end']} "
            f"(n_fired={w.get('n_test_fired')})"
        )
        if w["status"] != "ok":
            print(f"  status={w['status']} baseline_accuracy_test={_fmt_pct(w.get('baseline_accuracy_test'))}")
            continue
        auc = w.get("meta_auc_test")
        print(f"  meta_auc_test={auc:.4f}" if auc is not None else "  meta_auc_test=n/a")
        print(f"  baseline_accuracy_test={_fmt_pct(w['baseline_accuracy_test'])}")
        for tf in w["trust_filters"]:
            if np.isnan(tf["perm_p"]):
                print(f"    trust top {tf['frac']*100:.0f}%: n/a")
                continue
            print(
                f"    trust top {tf['frac']*100:.0f}% (n={tf['n_filtered']}): "
                f"filtered_accuracy={_fmt_pct(tf['filtered_accuracy'])} lift={_fmt_pct(tf['lift'])} "
                f"95%CI=[{_fmt_pct(tf['ci_lo'])}, {_fmt_pct(tf['ci_hi'])}] perm_p={tf['perm_p']:.4f}"
            )

    print("\n=== Delayed-action stress test (mirrors check_repricing_wall) ===")
    if stress.get("status") != "ok":
        print(f"  {stress.get('status')}")
        return
    b = stress["baseline_all_bars"]
    print(
        f"  baseline (ALL pooled test-fired bars, n={stress['n_pooled_test_fired']}): "
        f"status={b.get('status')} acc_fire={_fmt_pct(b.get('accuracy_at_fire'))} "
        f"acc_d1={_fmt_pct(b.get('accuracy_delay_1'))} acc_d2={_fmt_pct(b.get('accuracy_delay_2'))}"
    )
    for frac, r in stress["trust_filters"].items():
        print(
            f"  trust top {float(frac)*100:.0f}% (n_valid={r.get('n_valid')}): status={r.get('status')} "
            f"acc_fire={_fmt_pct(r.get('accuracy_at_fire'))} acc_d1={_fmt_pct(r.get('accuracy_delay_1'))} "
            f"acc_d2={_fmt_pct(r.get('accuracy_delay_2'))}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-csv", default=".run/training_view/training_view_nifty_direction_condition.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data", help="Needed only for the delayed-action stress test")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=5)
    ap.add_argument("--min-train-days", type=int, default=200)
    ap.add_argument("--trust-fracs", default=",".join(str(f) for f in DEFAULT_TRUST_FRACTIONS))
    ap.add_argument("--stress-test-edge-threshold", type=float, default=0.55)
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    trust_fracs = tuple(float(f) for f in args.trust_fracs.split(","))

    log.info("loading %s", args.input_csv)
    df = pd.read_csv(args.input_csv)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates (%s..%s)", len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())

    df = compute_composite_calls(df)
    df["composite_correct"] = compute_composite_correct(df["composite_direction"], df["payoff_winning_side"])
    df[BARS_SINCE_ACTIVE_COL] = add_bars_since_active(df).to_numpy()
    log.info(
        "composite fired on %d/%d bars (%.1f%%), unconditional accuracy over fired+labeled bars: %.4f",
        int(df["composite_fired"].sum()), len(df), 100.0 * df["composite_fired"].mean(),
        float(df["composite_correct"].mean()),
    )

    windows = build_expanding_windows(
        sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days,
    )
    log.info("built %d walk-forward windows", len(windows))

    window_results = [evaluate_window(df, w, trust_fracs=trust_fracs) for w in windows]

    stress: Dict[str, Any] = {"status": "skipped_by_flag"}
    if not args.skip_stress_test:
        stress = run_delayed_stress_test(
            window_results, args.parquet_base, horizon_bars=args.horizon_bars, lot_size=args.lot_size,
            trust_fracs=trust_fracs, edge_threshold=args.stress_test_edge_threshold,
        )

    print_report(window_results, stress)

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({"windows": _json_safe(window_results), "delayed_stress_test": _json_safe(stress)}, indent=2)
        )
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
