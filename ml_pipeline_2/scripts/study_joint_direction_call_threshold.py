"""Refinement of the joint direction model's CALL signal (NIFTY,
2026-09-27) -- NOT a new architecture. Reuses track 6's exact joint
multiclass model (``ml_pipeline_2.pipeline.joint_direction`` /
``ml_pipeline_2.scripts.train_joint_direction_v1``) and its own walk-forward
windows verbatim, and asks three narrower, previously-unanswered questions
per the owning task:

1. Track 6's own trading rule was ``argmax`` -- treating P(CALL)=34%
   (barely above the 3-way baseline of 1/3) identically to P(CALL)=90%, as
   long as it's the largest of the three. This throws away exactly the
   information a real trading rule needs. This script instead sweeps
   P(CALL) as a plain ABSOLUTE score threshold (independent of whether CALL
   happens to be the argmax) across a real grid, and asks: is there a
   single threshold value that is profitable CONSISTENTLY across most/all
   of the 5 independent walk-forward windows -- not just in the pooled
   average, which track 6's own numbers already showed can be dominated by
   one thin, early window (window 1: n=250 top-decile-of-argmax-CALL bars,
   +5.72% mean) while the three largest, most recent windows were
   flat-to-negative (-1.76%, +0.04%, -0.05%).

2. The ONE validated positive result in this project's entire research arc
   (``ml_pipeline_2.scripts.train_entry_option_payoff_v1``, holdout
   Spearman rho=0.154, top-decile mean +36%) has never been combined with
   this joint model's CALL signal. This script fits that SAME
   architecture (via ``entry_quality_direction.fit_entry_payoff_model`` /
   ``score_entry_payoff``, same ``ENTRY_FEATURES_V3`` feature set,
   ``payoff_score`` target) walk-forward-safely per window -- fit on the
   window's own TRAIN slice only, scored out-of-sample on TEST, exactly
   mirroring ``study_entry_quality_conditioned_direction.py``'s own
   convention -- rather than reusing the ONE fixed-split pinned bundle
   (whose train range, 2024-11-04..2026-04-30, would overlap windows 1-3's
   test periods and contaminate any comparison there). Restricting to bars
   where this independently-fit entry-timing model ALSO rates the bar in
   its own top quartile/decile (``joint_direction_threshold
   .entry_quality_mask``), does a Part-1 CALL threshold become MORE
   consistently profitable across windows?

3. Diagnoses WHY window 1 differs so much from windows 3-5: a regime check
   (VIX level/stability, ADX) per window, and a concentration check on
   window 1's own top-decile-of-argmax-CALL bars (how many distinct
   trade_dates, how expiry-day-heavy) -- distinguishing a real, if smaller,
   signal that later windows dilute from a thin/concentrated-sample
   artifact this project has been burned by before.

Import-isolation constraint (matches every sibling direction-research
module today): must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime``. Must NEVER touch
``.env.compose``/``docker-compose*.yml``/any deployed model bundle/
``verified_candidates.py``. Registry-recorded research only.

Compute note: this REFITS the joint multiclass model per window (Optuna HPO,
same as track 6 -- track 6 only persisted its JSON summary, not the fitted
model objects, and per-bar P(CALL) for every test row -- not just the
argmax==CALL subset -- is exactly what this script needs and track 6's
report never saved). Optuna's default TPE sampler is not seeded in
``train_joint_direction_v1.run_multiclass_hpo`` (reused verbatim, unmodified,
here), so this run's per-window numbers are an INDEPENDENT refit, not a
byte-identical reproduction of track 6's own figures -- material agreement
or disagreement with track 6's numbers is itself informative about how
stable this model class's structure is, and is reported as such.

Usage:
    python -m ml_pipeline_2.scripts.study_joint_direction_call_threshold \\
        --training-view-csv .run/training_view/training_view_nifty_direction_margin.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 --n-trials 20 \\
        --output-json .run/joint_direction_threshold/nifty_joint_direction_threshold_report.json
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
log = logging.getLogger("study_joint_direction_call_threshold")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.breakout_agreement_direction import leg_return_for_side  # noqa: E402
from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
)
from ml_pipeline_2.pipeline.direction_margin import carve_inner_validation  # noqa: E402
from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    compute_payoff_at_delay,
    fit_entry_payoff_model,
    score_entry_payoff,
)
from ml_pipeline_2.pipeline.joint_direction import (  # noqa: E402
    CALL,
    CLASS_TO_INT,
    argmax_class,
    class_balance,
    compute_three_class_label,
    top_decile_mask_within_argmax,
)
from ml_pipeline_2.pipeline.joint_direction_threshold import (  # noqa: E402
    consistency_summary,
    date_concentration,
    entry_quality_mask,
    evaluate_threshold_with_stats,
    near_expiry_share,
    pooled_percentile_thresholds,
    regime_diagnostics,
)
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402
from ml_pipeline_2.scripts.train_joint_direction_v1 import (  # noqa: E402
    DEFAULT_INNER_VALID_FRAC,
    DEFAULT_MIN_TEST_ROWS,
    DEFAULT_MIN_TRAIN_DAYS,
    DEFAULT_N_WINDOWS,
    JOINT_FEATURES,
    _fit_joint_model,
    _predict_proba,
)

DEFAULT_ENTRY_TOP_QUANTILES: List[float] = [0.75, 0.90]
DEFAULT_RECENT_N = 4
DEFAULT_GRID_N_BOOT = 1000
DEFAULT_GRID_N_PERM = 1000
DEFAULT_FINAL_N_BOOT = 5000
DEFAULT_FINAL_N_PERM = 5000
DEFAULT_MIN_ROWS = 20


# ── Per-window fit + score ──────────────────────────────────────────────────

def build_window_record(
    df: pd.DataFrame, window: WalkForwardWindow, *, n_trials: int, inner_valid_frac: float, min_test_rows: int,
) -> Dict[str, Any]:
    """Fit the joint model AND the entry-timing regressor on this window's
    own train slice (walk-forward-safe, no data past `window.train_end`
    touches either fit), then score BOTH on the window's full resolved test
    slice. Mirrors `train_joint_direction_v1.evaluate_window`'s window
    carve-out exactly (same `carve_inner_validation` split for the joint
    model's own HPO) plus the entry-timing model, fit fresh per window on
    the SAME window's full train range -- never the one fixed-split pinned
    bundle, whose 2024-11-04..2026-04-30 train range would overlap windows
    1-3's test periods here and silently contaminate the comparison."""
    train_df = df[df["trade_date"] <= window.train_end].reset_index(drop=True)
    test_df = df[(df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)].reset_index(drop=True)
    result: Dict[str, Any] = {
        "window": window.window_id, "train_start": window.train_start, "train_end": window.train_end,
        "test_start": window.test_start, "test_end": window.test_end, "n_train": len(train_df), "n_test": len(test_df),
    }
    if len(test_df) < min_test_rows:
        result["status"] = "thin_sample"
        return result

    train_dates = sorted(train_df["trade_date"].unique().tolist())
    try:
        inner_train_end, inner_valid_start = carve_inner_validation(train_dates, valid_frac=inner_valid_frac)
    except ValueError as exc:
        result["status"] = "thin_sample"
        result["reason"] = str(exc)
        return result
    inner_train_df = train_df[train_df["trade_date"] <= inner_train_end]
    inner_valid_df = train_df[train_df["trade_date"] >= inner_valid_start]

    fit = _fit_joint_model(inner_train_df, inner_valid_df, features=JOINT_FEATURES, n_trials=n_trials)
    if fit is None:
        result["status"] = "degenerate"
        result["reason"] = "not enough usable rows (both legs resolved) to fit the joint model on this window's inner train/valid split"
        return result

    test_label = compute_three_class_label(test_df["net_ce_return"], test_df["net_pe_return"])
    test_mask = test_label.notna().to_numpy()
    test_eval_df = test_df[test_mask].reset_index(drop=True)
    n_test_resolved = int(len(test_eval_df))
    if n_test_resolved < min_test_rows:
        result["status"] = "thin_sample"
        result["reason"] = f"only {n_test_resolved} resolved test rows after dropping unresolved-label bars"
        return result

    proba = _predict_proba(fit, test_eval_df, features=JOINT_FEATURES)
    argmax_labels = argmax_class(proba)
    proba_call = proba[:, CLASS_TO_INT[CALL]]
    net_ce = pd.to_numeric(test_eval_df["net_ce_return"], errors="coerce").to_numpy(dtype=float)
    net_pe = pd.to_numeric(test_eval_df["net_pe_return"], errors="coerce").to_numpy(dtype=float)

    entry_model, entry_medians = fit_entry_payoff_model(train_df, list(ENTRY_FEATURES_V3), "payoff_score")
    entry_score = score_entry_payoff(entry_model, entry_medians, test_eval_df, list(ENTRY_FEATURES_V3))

    result.update({
        "status": "ok",
        "n_test_resolved": n_test_resolved,
        "test_class_balance": class_balance(test_label[test_mask].to_numpy()),
        "joint_model_meta": {k: v for k, v in fit.items() if k not in ("model", "medians")},
        "_trade_date": test_eval_df["trade_date"].astype(str).to_numpy(),
        "_bar_index": test_eval_df["bar_index"].to_numpy(),
        "_dte": pd.to_numeric(test_eval_df.get("dte"), errors="coerce").to_numpy(dtype=float) if "dte" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        "_vix": pd.to_numeric(test_eval_df.get("vix_current"), errors="coerce").to_numpy(dtype=float) if "vix_current" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        "_adx": pd.to_numeric(test_eval_df.get("adx_14"), errors="coerce").to_numpy(dtype=float) if "adx_14" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        "_proba_call": proba_call,
        "_argmax_labels": argmax_labels,
        "_net_ce": net_ce,
        "_net_pe": net_pe,
        "_entry_score": entry_score,
    })
    return result


# ── Part 1 + Part 2: threshold grid (optionally entry-filtered) ────────────

def run_threshold_grid(
    ok_windows: Sequence[Dict[str, Any]],
    thresholds: Sequence[float],
    *,
    base_masks: Optional[Sequence[np.ndarray]] = None,
    recent_n: int = DEFAULT_RECENT_N,
    min_rows: int = DEFAULT_MIN_ROWS,
    n_boot: int = DEFAULT_GRID_N_BOOT,
    n_perm: int = DEFAULT_GRID_N_PERM,
) -> List[Dict[str, Any]]:
    """One row per threshold: per-window `evaluate_threshold_with_stats`
    plus the pooled evaluation plus `consistency_summary` -- optionally
    restricted per-window to `base_masks[i]` (Part 2's entry-timing filter).
    `ok_windows` must be in chronological window order (so `recent_n` means
    what it says)."""
    if base_masks is None:
        base_masks = [None] * len(ok_windows)
    pooled_proba = np.concatenate([w["_proba_call"] for w in ok_windows])
    pooled_ce = np.concatenate([w["_net_ce"] for w in ok_windows])
    pooled_mask = None if base_masks[0] is None else np.concatenate([m for m in base_masks])

    rows: List[Dict[str, Any]] = []
    for t in thresholds:
        per_window = [
            evaluate_threshold_with_stats(
                w["_proba_call"], w["_net_ce"], t, base_mask=bm, min_rows=min_rows, n_boot=n_boot, n_perm=n_perm,
            )
            for w, bm in zip(ok_windows, base_masks)
        ]
        pooled = evaluate_threshold_with_stats(
            pooled_proba, pooled_ce, t, base_mask=pooled_mask, min_rows=min_rows, n_boot=n_boot, n_perm=n_perm,
        )
        consistency = consistency_summary(per_window, recent_n=recent_n)
        rows.append({"threshold": t, "per_window": per_window, "pooled": pooled, "consistency": consistency})
    return rows


# ── Part 3: regime + concentration diagnostics ──────────────────────────────

def build_window_diagnostics(ok_windows: Sequence[Dict[str, Any]], *, top_decile_frac: float = 0.10) -> List[Dict[str, Any]]:
    out = []
    for w in ok_windows:
        argmax_call_mask = w["_argmax_labels"] == CALL
        top_decile_mask = top_decile_mask_within_argmax(
            w["_proba_call"], w["_argmax_labels"], target_class=CALL, frac=top_decile_frac,
        )
        net_ce = w["_net_ce"]
        regime = regime_diagnostics(w["_vix"], w["_adx"])
        conc_all = date_concentration(w["_trade_date"], argmax_call_mask)
        conc_top = date_concentration(w["_trade_date"], top_decile_mask)
        expiry_all = near_expiry_share(w["_dte"], argmax_call_mask)
        expiry_top = near_expiry_share(w["_dte"], top_decile_mask)

        def _mean_of(mask: np.ndarray) -> Optional[float]:
            vals = net_ce[mask]
            vals = vals[~np.isnan(vals)]
            return round(float(vals.mean()), 5) if len(vals) else None

        out.append({
            "window": w["window"], "test_start": w["test_start"], "test_end": w["test_end"],
            "n_test_resolved": w["n_test_resolved"],
            "regime": regime,
            "argmax_call": {
                "n": int(argmax_call_mask.sum()), "mean_actual_net_ce_return": _mean_of(argmax_call_mask),
                "date_concentration": conc_all, "near_expiry": expiry_all,
            },
            "top_decile_of_argmax_call": {
                "n": int(top_decile_mask.sum()), "mean_actual_net_ce_return": _mean_of(top_decile_mask),
                "date_concentration": conc_top, "near_expiry": expiry_top,
            },
        })
    return out


# ── Stress test: arbitrary boolean mask over pooled test rows ──────────────

def run_mask_stress_test(
    pooled_trade_date: np.ndarray,
    pooled_bar_index: np.ndarray,
    mask: np.ndarray,
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    leg: str = "CE",
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    """The mandatory delayed-action stress test for an arbitrary fired-bar
    mask (not necessarily `top_decile_mask_within_argmax` -- Part 1/2's
    fired set is a plain absolute-threshold cut, optionally further
    restricted by the entry-timing filter). Same re-pricing convention as
    `train_joint_direction_v1.run_class_stress_test` / `train_direction
    _margin_v1.run_leg_stress_test`: re-price the SAME fired bars at +1/+2
    bars holding the SAME original exit bar, scored via
    `validation.check_repricing_wall`."""
    fired_idx = np.where(np.asarray(mask, dtype=bool))[0]
    n = len(fired_idx)
    log.info("mask stress test: %d fired bars (leg=%s) pooled across OK windows", n, leg)
    out_return: Dict[int, List[Optional[float]]] = {0: [None] * n, 1: [None] * n, 2: [None] * n}
    by_date: Dict[str, List[int]] = {}
    for pos, idx in enumerate(fired_idx):
        by_date.setdefault(str(pooled_trade_date[idx]), []).append(pos)

    for trade_date, positions in by_date.items():
        snaps = read_day_snapshots(parquet_base, trade_date)
        if not snaps:
            continue
        for pos in positions:
            idx = fired_idx[pos]
            bar_index = int(pooled_bar_index[idx])
            for delay in (0, 1, 2):
                bp = compute_payoff_at_delay(snaps, bar_index, horizon_bars=horizon_bars, delay_bars=delay, lot_size=lot_size)
                if bp is None:
                    continue
                out_return[delay][pos] = leg_return_for_side(bp, leg)

    report = check_repricing_wall(
        payoff_at_fire=out_return[0], payoff_delay_1=out_return[1], payoff_delay_2=out_return[2],
        min_paired_bars=min_paired_bars, raise_on_fail=False,
    )
    from dataclasses import asdict
    return {"n_fired": n, "leg": leg, **asdict(report)}


# ── Reporting ────────────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            (str(k) if not isinstance(k, str) else k): _json_safe(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and (k.startswith("_") or k in ("model", "medians")))
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


def _find_consistent_candidates(grid_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in grid_rows:
        c = row["consistency"]
        if c["all_windows_positive"] or c["recent_all_positive"]:
            out.append(row)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=".run/training_view/training_view_nifty_direction_margin.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--inner-valid-frac", type=float, default=DEFAULT_INNER_VALID_FRAC)
    ap.add_argument("--min-test-rows", type=int, default=DEFAULT_MIN_TEST_ROWS)
    ap.add_argument("--n-trials", type=int, default=20)
    ap.add_argument("--recent-n", type=int, default=DEFAULT_RECENT_N)
    ap.add_argument("--entry-top-quantiles", default=",".join(str(q) for q in DEFAULT_ENTRY_TOP_QUANTILES))
    ap.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    ap.add_argument("--grid-n-boot", type=int, default=DEFAULT_GRID_N_BOOT)
    ap.add_argument("--grid-n-perm", type=int, default=DEFAULT_GRID_N_PERM)
    ap.add_argument("--final-n-boot", type=int, default=DEFAULT_FINAL_N_BOOT)
    ap.add_argument("--final-n-perm", type=int, default=DEFAULT_FINAL_N_PERM)
    ap.add_argument("--top-decile-frac", type=float, default=0.10)
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--stress-test-min-paired-bars", type=int, default=20)
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    entry_top_quantiles = [float(q.strip()) for q in args.entry_top_quantiles.split(",") if q.strip()]

    log.info("loading %s", args.training_view_csv)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates (%s..%s)", len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())

    windows = build_expanding_windows(sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days)
    log.info("built %d walk-forward windows", len(windows))

    window_records: List[Dict[str, Any]] = []
    for w in windows:
        log.info("=== window %d: train <= %s, test %s..%s ===", w.window_id, w.train_end, w.test_start, w.test_end)
        rec = build_window_record(df, w, n_trials=args.n_trials, inner_valid_frac=args.inner_valid_frac, min_test_rows=args.min_test_rows)
        log.info("window %d status=%s", w.window_id, rec.get("status"))
        window_records.append(rec)

    ok_windows = [w for w in window_records if w.get("status") == "ok"]
    if not ok_windows:
        log.error("no OK windows -- cannot run threshold sweep")
        report = {"windows": _json_safe(window_records), "error": "no OK windows"}
        if args.output_json:
            Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output_json).write_text(json.dumps(report, indent=2, default=str))
        return 1

    pooled_proba_call = np.concatenate([w["_proba_call"] for w in ok_windows])
    thresholds = pooled_percentile_thresholds(pooled_proba_call)
    log.info("threshold grid (pooled percentiles 50-99): %s", thresholds)

    # ── Part 1 ──────────────────────────────────────────────────────────────
    log.info("=== PART 1: absolute P(CALL) threshold sweep (no entry filter) ===")
    part1_grid = run_threshold_grid(
        ok_windows, thresholds, recent_n=args.recent_n, min_rows=args.min_rows,
        n_boot=args.grid_n_boot, n_perm=args.grid_n_perm,
    )
    part1_candidates = _find_consistent_candidates(part1_grid)
    log.info("Part 1: %d/%d thresholds are consistently positive (all-5 or most-recent-%d)", len(part1_candidates), len(thresholds), args.recent_n)

    # ── Part 2 ──────────────────────────────────────────────────────────────
    log.info("=== PART 2: entry-timing top-quantile filter + P(CALL) threshold ===")
    part2: Dict[float, List[Dict[str, Any]]] = {}
    part2_candidates: Dict[float, List[Dict[str, Any]]] = {}
    for q in entry_top_quantiles:
        window_entry_masks = [entry_quality_mask(w["_entry_score"], q) for w in ok_windows]
        grid_q = run_threshold_grid(
            ok_windows, thresholds, base_masks=window_entry_masks, recent_n=args.recent_n, min_rows=args.min_rows,
            n_boot=args.grid_n_boot, n_perm=args.grid_n_perm,
        )
        part2[q] = grid_q
        cands = _find_consistent_candidates(grid_q)
        part2_candidates[q] = cands
        log.info("Part 2 (entry top-quantile=%.2f): %d/%d thresholds consistently positive", q, len(cands), len(thresholds))

    # ── High-precision refinement of any candidates found ───────────────────
    headline_candidates: List[Dict[str, Any]] = []
    for row in part1_candidates:
        t = row["threshold"]
        pooled_precise = evaluate_threshold_with_stats(
            pooled_proba_call, np.concatenate([w["_net_ce"] for w in ok_windows]), t,
            min_rows=args.min_rows, n_boot=args.final_n_boot, n_perm=args.final_n_perm,
        )
        headline_candidates.append({"source": "part1_no_filter", "entry_top_quantile": None, "threshold": t, "pooled_precise": pooled_precise, "consistency": row["consistency"]})
    for q, cands in part2_candidates.items():
        window_entry_masks = [entry_quality_mask(w["_entry_score"], q) for w in ok_windows]
        pooled_mask = np.concatenate(window_entry_masks)
        for row in cands:
            t = row["threshold"]
            pooled_precise = evaluate_threshold_with_stats(
                pooled_proba_call, np.concatenate([w["_net_ce"] for w in ok_windows]), t, base_mask=pooled_mask,
                min_rows=args.min_rows, n_boot=args.final_n_boot, n_perm=args.final_n_perm,
            )
            headline_candidates.append({"source": f"part2_entry_top_quantile_{q}", "entry_top_quantile": q, "threshold": t, "pooled_precise": pooled_precise, "consistency": row["consistency"]})

    # ── Part 3: diagnostics ──────────────────────────────────────────────────
    log.info("=== PART 3: window regime + concentration diagnostics ===")
    window_diagnostics = build_window_diagnostics(ok_windows, top_decile_frac=args.top_decile_frac)

    # ── Mandatory delayed-action stress test on the best headline candidate ─
    stress_test_result: Optional[Dict[str, Any]] = None
    if not args.skip_stress_test and headline_candidates:
        best = max(headline_candidates, key=lambda h: (h["pooled_precise"].get("mean_actual") or -999))
        log.info("running MANDATORY delayed-action stress test on best headline candidate: %s", {k: v for k, v in best.items() if k != "pooled_precise"})
        pooled_trade_date = np.concatenate([w["_trade_date"] for w in ok_windows])
        pooled_bar_index = np.concatenate([w["_bar_index"] for w in ok_windows])
        pooled_proba = pooled_proba_call
        if best["entry_top_quantile"] is not None:
            base_mask = np.concatenate([entry_quality_mask(w["_entry_score"], best["entry_top_quantile"]) for w in ok_windows])
        else:
            base_mask = np.ones(len(pooled_proba), dtype=bool)
        fire_mask = base_mask & (pooled_proba >= best["threshold"])
        stress_test_result = run_mask_stress_test(
            pooled_trade_date, pooled_bar_index, fire_mask, args.parquet_base,
            horizon_bars=args.horizon_bars, lot_size=args.lot_size, leg="CE",
            min_paired_bars=args.stress_test_min_paired_bars,
        )
        stress_test_result["candidate"] = {k: v for k, v in best.items() if k != "pooled_precise"}

    report = {
        "config": {
            "training_view_csv": args.training_view_csv, "lot_size": args.lot_size, "horizon_bars": args.horizon_bars,
            "n_windows": args.n_windows, "min_train_days": args.min_train_days, "n_trials": args.n_trials,
            "recent_n": args.recent_n, "entry_top_quantiles": entry_top_quantiles, "thresholds": thresholds,
        },
        "windows": _json_safe(window_records),
        "part1_no_filter_grid": _json_safe(part1_grid),
        "part1_consistent_thresholds": _json_safe(part1_candidates),
        "part2_entry_filtered_grid": _json_safe(part2),
        "part2_consistent_thresholds": _json_safe(part2_candidates),
        "headline_candidates": _json_safe(headline_candidates),
        "window_diagnostics": _json_safe(window_diagnostics),
        "stress_test": _json_safe(stress_test_result) if stress_test_result is not None else None,
    }

    print("\n" + "=" * 78)
    print("=== PART 1: is ANY absolute P(CALL) threshold consistent across windows? ===")
    print("=" * 78)
    for row in part1_grid:
        c = row["consistency"]
        print(f"thr={row['threshold']:.4f}  means_by_window={c['means_by_window']}  "
              f"all_positive={c['all_windows_positive']}  recent{args.recent_n}_positive={c['recent_all_positive']}  "
              f"pooled_mean={row['pooled'].get('mean_actual')}  pooled_p={row['pooled'].get('permutation_p_value')}")
    print(f"\n-> {len(part1_candidates)} / {len(thresholds)} thresholds are consistently positive (Part 1, no entry filter)")

    for q in entry_top_quantiles:
        print("\n" + "=" * 78)
        print(f"=== PART 2: entry-timing top-quantile={q:.2f} filter + P(CALL) threshold ===")
        print("=" * 78)
        for row in part2[q]:
            c = row["consistency"]
            print(f"thr={row['threshold']:.4f}  means_by_window={c['means_by_window']}  "
                  f"all_positive={c['all_windows_positive']}  recent{args.recent_n}_positive={c['recent_all_positive']}  "
                  f"pooled_mean={row['pooled'].get('mean_actual')}  pooled_n={row['pooled'].get('n_valid')}")
        print(f"\n-> {len(part2_candidates[q])} / {len(thresholds)} thresholds consistently positive at entry_top_quantile={q:.2f}")

    print("\n" + "=" * 78)
    print("=== HEADLINE CANDIDATES (thresholds/filters that were consistent) ===")
    print("=" * 78)
    if headline_candidates:
        for h in headline_candidates:
            print(f"  source={h['source']} threshold={h['threshold']:.4f} entry_top_quantile={h['entry_top_quantile']} "
                  f"pooled_mean={h['pooled_precise'].get('mean_actual')} ci=({h['pooled_precise'].get('ci_lo')},{h['pooled_precise'].get('ci_hi')}) "
                  f"p={h['pooled_precise'].get('permutation_p_value')} n_valid={h['pooled_precise'].get('n_valid')}")
    else:
        print("  NONE -- no threshold (with or without the entry-timing filter) was consistently positive across all-5 or the most recent windows.")

    print("\n" + "=" * 78)
    print("=== PART 3: window regime + concentration diagnostics ===")
    print("=" * 78)
    for d in window_diagnostics:
        print(f"\nWindow {d['window']} ({d['test_start']}..{d['test_end']}, n_test_resolved={d['n_test_resolved']}):")
        print(f"  regime: {d['regime']}")
        print(f"  argmax==CALL: n={d['argmax_call']['n']} mean_actual={d['argmax_call']['mean_actual_net_ce_return']} "
              f"n_distinct_dates={d['argmax_call']['date_concentration']['n_distinct_dates']} top_date_share={d['argmax_call']['date_concentration']['top_date_share']} "
              f"expiry_baseline={d['argmax_call']['near_expiry']['baseline_share']} expiry_masked={d['argmax_call']['near_expiry']['masked_share']}")
        print(f"  top-decile-of-argmax-CALL: n={d['top_decile_of_argmax_call']['n']} mean_actual={d['top_decile_of_argmax_call']['mean_actual_net_ce_return']} "
              f"n_distinct_dates={d['top_decile_of_argmax_call']['date_concentration']['n_distinct_dates']} top_date_share={d['top_decile_of_argmax_call']['date_concentration']['top_date_share']} "
              f"expiry_baseline={d['top_decile_of_argmax_call']['near_expiry']['baseline_share']} expiry_masked={d['top_decile_of_argmax_call']['near_expiry']['masked_share']}")

    if stress_test_result is not None:
        print("\n" + "=" * 78)
        print("=== MANDATORY DELAYED-ACTION STRESS TEST (best headline candidate) ===")
        print("=" * 78)
        print(json.dumps(_json_safe(stress_test_result), indent=2, default=str))

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
