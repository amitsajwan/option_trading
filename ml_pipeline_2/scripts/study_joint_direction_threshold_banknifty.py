"""Absolute-probability threshold calibration for BankNifty's joint direction
model (2026-09-28) -- refinement of ``train_joint_direction_banknifty_v1``'s
result, generalized from NIFTY's own refinement
(``ml_pipeline_2.pipeline.joint_direction_threshold`` /
``ml_pipeline_2.scripts.study_joint_direction_call_threshold``, 2026-09-27)
to run against WHICHEVER class (CALL or PUT, chosen via ``--target-class``)
BankNifty's own per-window/pooled decile-lift tables show a positive result
for -- unlike NIFTY, where only CALL cleared the pooled ship gates and the
refinement was hardcoded to it.

WHY THIS SCRIPT EXISTS (mandatory skepticism, per this task): NIFTY's joint
model's headline CALL number (pooled top-decile mean positive, Spearman rho
positive in 5/5 windows) used ``argmax`` as the trading rule, which turned
out to hide that the ENTIRE pooled effect was driven by one thin, early
window (250 bars) while the three largest, most recent windows were
flat-to-negative -- and an independent refit (this script's own NIFTY
ancestor) with a different, unseeded Optuna HPO run flipped that window's
sign entirely, closing the result out as noise. This script applies the
EXACT same two-part scrutiny to BankNifty's result on whichever class looked
promising:
  Part 1: sweep P(class) as a plain ABSOLUTE threshold (independent of
    argmax) across a grid fixed on the POOLED probability distribution, and
    check whether any single value is profitable in ALL 5 (or at least the
    most recent 4) walk-forward windows separately -- not just on average.
  Part 2: additionally require BankNifty's own entry-timing model (refit
    walk-forward-safely per window, same architecture as
    ``train_entry_option_payoff_v1``, the ONE validated positive result this
    project has -- see ``entry_quality_direction.fit_entry_payoff_model``)
    to ALSO rate the bar in its own top quartile/decile: does combining the
    two signals turn a marginal threshold into a consistently profitable
    one.
  Part 3: regime (VIX/ADX) and date-concentration/near-expiry diagnostics
    per window, to catch a thin/concentrated-sample artifact before it's
    ever reported as a real result.
A candidate surviving Parts 1-3 still needs the SEED-SENSITIVITY check this
task's item 4(c) makes mandatory (this script does not do that itself --
run this study script's OWN unseeded Optuna refit for the SAME window a
second time, matching NIFTY's own version of this check, and compare signs;
see the owning task's final report for that specific result) before the
mandatory delayed-action stress test (Part 4, run automatically here on the
single best headline candidate, mirroring the NIFTY script).

Reuse, not re-derivation: ``run_threshold_grid`` and
``_find_consistent_candidates`` are imported UNMODIFIED from
``study_joint_direction_call_threshold`` -- both are already fully generic
over the window dict's ``_proba_call``/``_net_ce`` keys (they never inspect
CALL-ness itself, just read those two keys by name), so this script's own
``build_window_record`` below populates those exact same keys with whichever
class ``--target-class`` selects (CALL -> P(CALL)/net_ce_return,
PUT -> P(PUT)/net_pe_return) rather than duplicating the grid-orchestration
logic under a renamed key. ``run_mask_stress_test`` is likewise imported
unmodified (already parameterized by ``leg``). Only ``build_window_record``
and ``build_window_diagnostics`` are genuinely new here (the original
NIFTY versions hardcode ``CALL``/``CLASS_TO_INT[CALL]``/``net_ce_return``
throughout) -- these two get their own dedicated tests, since everything
else is either imported verbatim (already tested) or a thin CLI wrapper.

Import-isolation constraint (matches every sibling direction-research
module): must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime``. Must NEVER touch
``.env.compose``/``docker-compose*.yml``/any deployed model bundle/
``verified_candidates.py``. Registry-recorded research only.

Usage:
    python -m ml_pipeline_2.scripts.study_joint_direction_threshold_banknifty \\
        --training-view-csv .run/training_view/training_view_banknifty_direction_margin_jointdir.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data_banknifty \\
        --lot-size 30 --horizon-bars 15 --target-class CALL \\
        --n-windows 5 --min-train-days 200 --n-trials 20 \\
        --output-json .run/joint_direction_threshold_bn/banknifty_joint_direction_threshold_call_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("study_joint_direction_threshold_banknifty")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
)
from ml_pipeline_2.pipeline.direction_margin import carve_inner_validation  # noqa: E402
from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    fit_entry_payoff_model,
    score_entry_payoff,
)
from ml_pipeline_2.pipeline.joint_direction import (  # noqa: E402
    CALL,
    CLASS_TO_INT,
    PUT,
    argmax_class,
    class_balance,
    compute_three_class_label,
    top_decile_mask_within_argmax,
)
from ml_pipeline_2.pipeline.joint_direction_threshold import (  # noqa: E402
    date_concentration,
    entry_quality_mask,
    near_expiry_share,
    pooled_percentile_thresholds,
    regime_diagnostics,
)
from ml_pipeline_2.scripts.study_joint_direction_call_threshold import (  # noqa: E402
    _find_consistent_candidates,
    run_mask_stress_test,
    run_threshold_grid,
)
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

TARGET_CLASSES = (CALL, PUT)
LEG_FOR_CLASS: Dict[str, str] = {CALL: "CE", PUT: "PE"}
NET_RETURN_COL_FOR_CLASS: Dict[str, str] = {CALL: "net_ce_return", PUT: "net_pe_return"}

DEFAULT_ENTRY_TOP_QUANTILES: List[float] = [0.75, 0.90]
DEFAULT_RECENT_N = 4
DEFAULT_GRID_N_BOOT = 1000
DEFAULT_GRID_N_PERM = 1000
DEFAULT_FINAL_N_BOOT = 5000
DEFAULT_FINAL_N_PERM = 5000
DEFAULT_MIN_ROWS = 20

BANKNIFTY_DEFAULT_TRAINING_VIEW = ".run/training_view/training_view_banknifty_direction_margin_jointdir.csv.gz"
BANKNIFTY_DEFAULT_PARQUET_BASE = ".data/ml_pipeline/parquet_data_banknifty"
BANKNIFTY_DEFAULT_LOT_SIZE = 30
BANKNIFTY_DEFAULT_HORIZON_BARS = 15


# ── Per-window fit + score (generalized over target_class) ─────────────────

def build_window_record(
    df: pd.DataFrame, window: WalkForwardWindow, *, target_class: str, n_trials: int, inner_valid_frac: float, min_test_rows: int,
) -> Dict[str, Any]:
    """Same walk-forward carve-out as
    ``study_joint_direction_call_threshold.build_window_record`` (fits the
    joint model AND the entry-timing regressor fresh on this window's own
    train slice, scores both out-of-sample on test), generalized to whichever
    ``target_class`` (CALL or PUT) is under study. Populates the window dict
    with ``_proba_call``/``_net_ce`` KEY NAMES regardless of `target_class`
    -- see module docstring for why: `run_threshold_grid` (imported
    unmodified from the NIFTY/CALL-only script) only ever reads those two
    keys by name, never inspects CALL-ness, so this is a deliberate,
    documented field-name reuse to avoid a second copy of that grid-
    orchestration logic, not an accidental CALL-only leftover."""
    if target_class not in TARGET_CLASSES:
        raise ValueError(f"build_window_record: target_class must be one of {TARGET_CLASSES}, got {target_class!r}")
    net_col = NET_RETURN_COL_FOR_CLASS[target_class]

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
    proba_target = proba[:, CLASS_TO_INT[target_class]]
    net_target = pd.to_numeric(test_eval_df[net_col], errors="coerce").to_numpy(dtype=float)

    entry_model, entry_medians = fit_entry_payoff_model(train_df, list(ENTRY_FEATURES_V3), "payoff_score")
    entry_score = score_entry_payoff(entry_model, entry_medians, test_eval_df, list(ENTRY_FEATURES_V3))

    result.update({
        "status": "ok",
        "target_class": target_class,
        "n_test_resolved": n_test_resolved,
        "test_class_balance": class_balance(test_label[test_mask].to_numpy()),
        "joint_model_meta": {k: v for k, v in fit.items() if k not in ("model", "medians")},
        "_trade_date": test_eval_df["trade_date"].astype(str).to_numpy(),
        "_bar_index": test_eval_df["bar_index"].to_numpy(),
        "_dte": pd.to_numeric(test_eval_df.get("dte"), errors="coerce").to_numpy(dtype=float) if "dte" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        "_vix": pd.to_numeric(test_eval_df.get("vix_current"), errors="coerce").to_numpy(dtype=float) if "vix_current" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        "_adx": pd.to_numeric(test_eval_df.get("adx_14"), errors="coerce").to_numpy(dtype=float) if "adx_14" in test_eval_df.columns else np.full(n_test_resolved, np.nan),
        # Deliberately reused key names -- see docstring.
        "_proba_call": proba_target,
        "_net_ce": net_target,
        "_argmax_labels": argmax_labels,
        "_entry_score": entry_score,
    })
    return result


# ── Part 3: regime + concentration diagnostics (generalized) ───────────────

def build_window_diagnostics(
    ok_windows, *, target_class: str, top_decile_frac: float = 0.10,
) -> List[Dict[str, Any]]:
    """Generalized version of
    ``study_joint_direction_call_threshold.build_window_diagnostics`` --
    that function hardcodes ``CALL``/``net_ce_return`` throughout; this one
    keys everything off `target_class` instead (reads the `_proba_call`/
    `_net_ce` fields `build_window_record` populates above, which already
    hold the target class's own proba/actual values)."""
    if target_class not in TARGET_CLASSES:
        raise ValueError(f"build_window_diagnostics: target_class must be one of {TARGET_CLASSES}, got {target_class!r}")
    out = []
    for w in ok_windows:
        argmax_mask = w["_argmax_labels"] == target_class
        top_decile_mask = top_decile_mask_within_argmax(
            w["_proba_call"], w["_argmax_labels"], target_class=target_class, frac=top_decile_frac,
        )
        net_target = w["_net_ce"]
        regime = regime_diagnostics(w["_vix"], w["_adx"])
        conc_all = date_concentration(w["_trade_date"], argmax_mask)
        conc_top = date_concentration(w["_trade_date"], top_decile_mask)
        expiry_all = near_expiry_share(w["_dte"], argmax_mask)
        expiry_top = near_expiry_share(w["_dte"], top_decile_mask)

        def _mean_of(mask: np.ndarray) -> Optional[float]:
            vals = net_target[mask]
            vals = vals[~np.isnan(vals)]
            return round(float(vals.mean()), 5) if len(vals) else None

        out.append({
            "window": w["window"], "test_start": w["test_start"], "test_end": w["test_end"],
            "n_test_resolved": w["n_test_resolved"],
            "regime": regime,
            f"argmax_{target_class.lower()}": {
                "n": int(argmax_mask.sum()), "mean_actual_return": _mean_of(argmax_mask),
                "date_concentration": conc_all, "near_expiry": expiry_all,
            },
            f"top_decile_of_argmax_{target_class.lower()}": {
                "n": int(top_decile_mask.sum()), "mean_actual_return": _mean_of(top_decile_mask),
                "date_concentration": conc_top, "near_expiry": expiry_top,
            },
        })
    return out


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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=BANKNIFTY_DEFAULT_TRAINING_VIEW)
    ap.add_argument("--parquet-base", default=BANKNIFTY_DEFAULT_PARQUET_BASE)
    ap.add_argument("--lot-size", type=int, default=BANKNIFTY_DEFAULT_LOT_SIZE)
    ap.add_argument("--horizon-bars", type=int, default=BANKNIFTY_DEFAULT_HORIZON_BARS)
    ap.add_argument("--target-class", required=True, choices=list(TARGET_CLASSES))
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

    target_class = args.target_class
    leg = LEG_FOR_CLASS[target_class]
    entry_top_quantiles = [float(q.strip()) for q in args.entry_top_quantiles.split(",") if q.strip()]

    log.info("loading %s (target_class=%s, leg=%s)", args.training_view_csv, target_class, leg)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info("loaded %d rows, %d trade_dates (%s..%s)", len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())

    windows = build_expanding_windows(sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days)
    log.info("built %d walk-forward windows (BankNifty's own date range, not reused from NIFTY)", len(windows))

    window_records: List[Dict[str, Any]] = []
    for w in windows:
        log.info("=== window %d: train <= %s, test %s..%s ===", w.window_id, w.train_end, w.test_start, w.test_end)
        rec = build_window_record(df, w, target_class=target_class, n_trials=args.n_trials, inner_valid_frac=args.inner_valid_frac, min_test_rows=args.min_test_rows)
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

    pooled_proba_target = np.concatenate([w["_proba_call"] for w in ok_windows])
    thresholds = pooled_percentile_thresholds(pooled_proba_target)
    log.info("threshold grid (pooled percentiles 50-99): %s", thresholds)

    # ── Part 1 ──────────────────────────────────────────────────────────────
    log.info("=== PART 1: absolute P(%s) threshold sweep (no entry filter) ===", target_class)
    part1_grid = run_threshold_grid(
        ok_windows, thresholds, recent_n=args.recent_n, min_rows=args.min_rows,
        n_boot=args.grid_n_boot, n_perm=args.grid_n_perm,
    )
    part1_candidates = _find_consistent_candidates(part1_grid)
    log.info("Part 1: %d/%d thresholds are consistently positive (all-5 or most-recent-%d)", len(part1_candidates), len(thresholds), args.recent_n)

    # ── Part 2 ──────────────────────────────────────────────────────────────
    log.info("=== PART 2: entry-timing top-quantile filter + P(%s) threshold ===", target_class)
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
        pooled_precise = _precise_eval(pooled_proba_target, ok_windows, t, base_mask=None, args=args)
        headline_candidates.append({"source": "part1_no_filter", "entry_top_quantile": None, "threshold": t, "pooled_precise": pooled_precise, "consistency": row["consistency"]})
    for q, cands in part2_candidates.items():
        window_entry_masks = [entry_quality_mask(w["_entry_score"], q) for w in ok_windows]
        pooled_mask = np.concatenate(window_entry_masks)
        for row in cands:
            t = row["threshold"]
            pooled_precise = _precise_eval(pooled_proba_target, ok_windows, t, base_mask=pooled_mask, args=args)
            headline_candidates.append({"source": f"part2_entry_top_quantile_{q}", "entry_top_quantile": q, "threshold": t, "pooled_precise": pooled_precise, "consistency": row["consistency"]})

    # ── Part 3: diagnostics ──────────────────────────────────────────────────
    log.info("=== PART 3: window regime + concentration diagnostics ===")
    window_diagnostics = build_window_diagnostics(ok_windows, target_class=target_class, top_decile_frac=args.top_decile_frac)

    # ── Mandatory delayed-action stress test on the best headline candidate ─
    stress_test_result: Optional[Dict[str, Any]] = None
    if not args.skip_stress_test and headline_candidates:
        best = max(headline_candidates, key=lambda h: (h["pooled_precise"].get("mean_actual") or -999))
        log.info("running MANDATORY delayed-action stress test on best headline candidate: %s", {k: v for k, v in best.items() if k != "pooled_precise"})
        pooled_trade_date = np.concatenate([w["_trade_date"] for w in ok_windows])
        pooled_bar_index = np.concatenate([w["_bar_index"] for w in ok_windows])
        if best["entry_top_quantile"] is not None:
            base_mask = np.concatenate([entry_quality_mask(w["_entry_score"], best["entry_top_quantile"]) for w in ok_windows])
        else:
            base_mask = np.ones(len(pooled_proba_target), dtype=bool)
        fire_mask = base_mask & (pooled_proba_target >= best["threshold"])
        stress_test_result = run_mask_stress_test(
            pooled_trade_date, pooled_bar_index, fire_mask, args.parquet_base,
            horizon_bars=args.horizon_bars, lot_size=args.lot_size, leg=leg,
            min_paired_bars=args.stress_test_min_paired_bars,
        )
        stress_test_result["candidate"] = {k: v for k, v in best.items() if k != "pooled_precise"}

    report = {
        "config": {
            "instrument": "BANKNIFTY", "target_class": target_class, "leg": leg,
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
    print(f"=== BANKNIFTY PART 1: is ANY absolute P({target_class}) threshold consistent across windows? ===")
    print("=" * 78)
    for row in part1_grid:
        c = row["consistency"]
        print(f"thr={row['threshold']:.4f}  means_by_window={c['means_by_window']}  "
              f"all_positive={c['all_windows_positive']}  recent{args.recent_n}_positive={c['recent_all_positive']}  "
              f"pooled_mean={row['pooled'].get('mean_actual')}  pooled_p={row['pooled'].get('permutation_p_value')}")
    print(f"\n-> {len(part1_candidates)} / {len(thresholds)} thresholds are consistently positive (Part 1, no entry filter)")

    for q in entry_top_quantiles:
        print("\n" + "=" * 78)
        print(f"=== PART 2: entry-timing top-quantile={q:.2f} filter + P({target_class}) threshold ===")
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
        print(f"  NONE -- no threshold (with or without the entry-timing filter) was consistently positive for {target_class} across all-5 or the most recent windows.")

    print("\n" + "=" * 78)
    print("=== PART 3: window regime + concentration diagnostics ===")
    print("=" * 78)
    for d in window_diagnostics:
        argmax_key = f"argmax_{target_class.lower()}"
        top_key = f"top_decile_of_argmax_{target_class.lower()}"
        print(f"\nWindow {d['window']} ({d['test_start']}..{d['test_end']}, n_test_resolved={d['n_test_resolved']}):")
        print(f"  regime: {d['regime']}")
        print(f"  argmax=={target_class}: n={d[argmax_key]['n']} mean_actual={d[argmax_key]['mean_actual_return']} "
              f"n_distinct_dates={d[argmax_key]['date_concentration']['n_distinct_dates']} top_date_share={d[argmax_key]['date_concentration']['top_date_share']} "
              f"expiry_baseline={d[argmax_key]['near_expiry']['baseline_share']} expiry_masked={d[argmax_key]['near_expiry']['masked_share']}")
        print(f"  top-decile-of-argmax-{target_class}: n={d[top_key]['n']} mean_actual={d[top_key]['mean_actual_return']} "
              f"n_distinct_dates={d[top_key]['date_concentration']['n_distinct_dates']} top_date_share={d[top_key]['date_concentration']['top_date_share']} "
              f"expiry_baseline={d[top_key]['near_expiry']['baseline_share']} expiry_masked={d[top_key]['near_expiry']['masked_share']}")

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


def _precise_eval(pooled_proba: np.ndarray, ok_windows, threshold: float, *, base_mask: Optional[np.ndarray], args: argparse.Namespace) -> Dict[str, Any]:
    from ml_pipeline_2.pipeline.joint_direction_threshold import evaluate_threshold_with_stats

    pooled_net = np.concatenate([w["_net_ce"] for w in ok_windows])
    return evaluate_threshold_with_stats(
        pooled_proba, pooled_net, threshold, base_mask=base_mask,
        min_rows=args.min_rows, n_boot=args.final_n_boot, n_perm=args.final_n_perm,
    )


if __name__ == "__main__":
    raise SystemExit(main())
