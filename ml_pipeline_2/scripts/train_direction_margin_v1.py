"""Two INDEPENDENT XGBoost regressors (call-specific and put-specific), each
predicting its own leg's actual net (post-cost) return, walk-forward tested
across the same 5 expanding-origin windows every other direction-research
track today used.

REFRAMING (2026-09-27, project-owner correction -- read before trusting any
"direction" language elsewhere in this file's history/comments): this study
does NOT answer a "CE or PE, which wins" direction question. That framing
(`predicted_margin = call_pred - put_pred`, its sign as a direction call,
compared against the other ten direction-research tracks) is preserved
below ONLY as a clearly-labeled SECONDARY section, because the original
build already computed it and it is legitimate supporting context -- it is
never the headline. The actual, headline question is: are the call model
and the put model each, entirely on their own, a self-contained, tradeable
entry-timing-plus-side-decision strategy -- EXACTLY the same question
`train_entry_option_payoff_v1.py` answered for the direction-agnostic
`max(net_ce, net_pe)` label, just asked once for `net_ce_return` alone and
once for `net_pe_return` alone, with the two verdicts reported side by
side and NEVER combined into one number. This project has real, documented
CE/PE asymmetry (`docs/EXIT_RISK_EXPERIMENTS_2026-05.md`: CE leg PF 1.42 vs
PE leg PF 0.81 in the same window) -- an asymmetric result here (one leg
real, the other not) is a legitimate, expected-possible outcome, not
something to average away.

This is registry-recorded research only. It must NEVER import
`strategy_app.ml.entry_direction_resolver`,
`strategy_app.engines.strategies.ml_entry`, `strategy_app.brain
.regime_director`, or `strategy_app.market.regime`.

Methodology:
  1. Load `training_view_nifty_direction_margin.csv.gz` (built by
     `build_direction_margin_training_view.py` -- ENTRY_FEATURES_V3 +
     `atm_ce_return_1m`/`atm_pe_return_1m` + `net_ce_return`/`net_pe_return`
     + ten additional raw per-leg LEVEL features, see
     `direction_margin.LEG_RAW_FEATURE_COLUMNS`).
  2. Split into 5 expanding-origin walk-forward windows via
     `conditional_direction.build_expanding_windows`, SAME
     `min_train_days=200` every sibling track used on the same underlying
     history -- identical window boundaries (test windows
     2025-08-26..2026-07-31) for direct comparability.
  3. Per window: carve the window's own TRAIN date range into an inner-train
     tail and an inner-validation head (`direction_margin
     .carve_inner_validation`, by DATE fraction, never by row count), run an
     independent Optuna HPO (optimizing validation Spearman rho, exactly
     `train_entry_option_payoff_v1.run_hpo`, reused verbatim) for the CALL
     model (features: ENTRY_FEATURES_V3 + `CALL_ONLY_FEATURES`, target:
     `net_ce_return`, winsorized) and the PUT model (ENTRY_FEATURES_V3 +
     `PUT_ONLY_FEATURES`, target: `net_pe_return`, winsorized)
     INDEPENDENTLY -- different training rows can be dropped per model
     (whichever leg is illiquid on a given bar), different HPO, no shared
     parameters.
  4. HEADLINE: score each model on the window's full test slice against ITS
     OWN actual leg return (`net_ce_return` for the call model,
     `net_pe_return` for the put model, never the margin) -- Spearman rho +
     p-value, decile-lift table (reuses
     `train_entry_option_payoff_v1.decile_lift_table` verbatim), reported
     per window AND pooled. Each leg's pooled result is run through
     `train_entry_option_payoff_v1.compute_ship_gates` INDEPENDENTLY (same
     thresholds the validated entry-timing model itself had to clear:
     rows>=200, top-decile-mean>0, spearman_rho>=0.05, p<=0.05) plus a
     walk-forward-consistency check (fraction of OK windows with a
     positive own-leg Spearman rho). A leg that clears its ship gates gets
     the MANDATORY delayed-action stress test run on ITS OWN top-decile
     fires only, re-pricing ONLY that leg
     (`validation.check_repricing_wall`, exactly
     `run_repricing_wall_control.py`'s convention) -- the two legs' stress
     tests are fully independent; one can pass while the other fails.
  5. SECONDARY (kept, clearly labeled, never the headline): the original
     margin-level view -- `predicted_margin = call_pred - put_pred` vs
     `actual_margin = net_ce_return - net_pe_return` (Spearman + decile
     lift), plus the binary-equivalent `sign(predicted_margin)` vs
     `payoff_winning_side` accuracy (bootstrap CI + permutation p-value,
     `breakout_agreement_direction.evaluate_signal_accuracy`) for
     comparability with the four PRIOR direction-as-one-decision tracks,
     with its own (margin-framed) delayed-action stress test
     (`direction_margin.is_promising` gate, `conditional_direction
     .check_direction_repricing_wall` + `validation.check_repricing_wall`
     on the pooled two-tailed "confident calls" subset).

Usage:
    python -m ml_pipeline_2.scripts.train_direction_margin_v1 \\
        --training-view-csv .run/training_view/training_view_nifty_direction_margin.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 --n-trials 20 \\
        --output-json .run/direction_margin/nifty_direction_margin_report.json
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
log = logging.getLogger("train_direction_margin_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.breakout_agreement_direction import evaluate_signal_accuracy, leg_return_for_side  # noqa: E402
from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
    check_direction_repricing_wall,
)
from ml_pipeline_2.pipeline.direction_margin import (  # noqa: E402
    CALL_ONLY_FEATURES,
    PUT_ONLY_FEATURES,
    called_leg_actual_return,
    carve_inner_validation,
    compute_actual_margin,
    compute_predicted_margin,
    is_promising,
    margin_to_side,
    select_confident_subset_mask,
)
from ml_pipeline_2.pipeline.entry_quality_direction import compute_payoff_at_delay  # noqa: E402
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402
from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import (  # noqa: E402
    compute_ship_gates,
    decile_lift_table,
    run_hpo,
    winsorize_target,
)

CALL_FEATURES: List[str] = list(ENTRY_FEATURES_V3) + list(CALL_ONLY_FEATURES)
PUT_FEATURES: List[str] = list(ENTRY_FEATURES_V3) + list(PUT_ONLY_FEATURES)

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_INNER_VALID_FRAC = 0.15
DEFAULT_MIN_TEST_ROWS = 30
DEFAULT_MIN_FIT_ROWS = 200
DEFAULT_CONFIDENT_FRAC = 0.10
DEFAULT_EDGE_THRESHOLD = 0.55


def _select_features(df: pd.DataFrame, features: Sequence[str]) -> pd.DataFrame:
    missing = [f for f in features if f not in df.columns]
    if missing:
        raise ValueError(f"training view is missing expected feature columns: {missing}")
    return df[list(features)].apply(pd.to_numeric, errors="coerce")


def _regression_eval(predicted: np.ndarray, actual: np.ndarray, *, n_deciles: int = 10) -> Dict[str, Any]:
    """Spearman rho/p + decile lift table of ACTUAL values sorted by
    PREDICTED, over rows where `actual` is non-NaN -- generic enough to
    score the call leg, the put leg, or the margin, so it's written ONCE
    here rather than duplicated three times."""
    from scipy.stats import spearmanr

    mask = ~np.isnan(actual)
    n = int(mask.sum())
    if n < 2:
        return {"rows": n, "spearman_rho": None, "spearman_p_value": None, "decile_table": [],
                "top_decile_mean": None, "bottom_decile_mean": None}
    rho, p_value = spearmanr(predicted[mask], actual[mask])
    rho = 0.0 if np.isnan(rho) else float(rho)
    p_value = 1.0 if np.isnan(p_value) else float(p_value)
    deciles = decile_lift_table(predicted[mask], actual[mask], n_deciles=n_deciles)
    return {
        "rows": n,
        "spearman_rho": round(rho, 4),
        "spearman_p_value": round(p_value, 4),
        "decile_table": deciles,
        "top_decile_mean": deciles[-1]["mean_actual_payoff"] if deciles else None,
        "bottom_decile_mean": deciles[0]["mean_actual_payoff"] if deciles else None,
    }


def _fit_leg_model(
    train_df: pd.DataFrame, valid_df: pd.DataFrame, *, features: Sequence[str], target_col: str, n_trials: int,
) -> Optional[Dict[str, Any]]:
    """Fit one leg's independent XGBoost regressor via HPO on its own
    (train, valid) slice, dropping rows where THIS leg's target is missing
    -- returns None (caller marks the window degenerate for this leg) if
    there aren't enough usable rows on either side of the split."""
    tr = train_df.dropna(subset=[target_col])
    va = valid_df.dropna(subset=[target_col])
    if len(tr) < DEFAULT_MIN_FIT_ROWS or len(va) < max(20, int(DEFAULT_MIN_FIT_ROWS * 0.1)):
        return None
    X_train = _select_features(tr, features)
    y_train_raw = pd.to_numeric(tr[target_col], errors="coerce").to_numpy()
    X_valid = _select_features(va, features)
    y_valid = pd.to_numeric(va[target_col], errors="coerce").to_numpy()

    y_train, wlo, whi = winsorize_target(y_train_raw)
    medians = X_train.median(numeric_only=True)
    X_train = X_train.fillna(medians)
    X_valid = X_valid.fillna(medians)

    model, best_params, best_valid_rho = run_hpo(X_train, y_train, X_valid, y_valid, n_trials=n_trials)
    importances: Dict[str, float] = {}
    try:
        for feat, imp in zip(features, model.feature_importances_):
            importances[feat] = round(float(imp), 6)
    except Exception:
        pass
    return {
        "model": model,
        "medians": medians,
        "n_train": int(len(tr)),
        "n_valid": int(len(va)),
        "winsorize_bounds": (wlo, whi),
        "best_params": best_params,
        "best_valid_spearman_rho": round(best_valid_rho, 4),
        "feature_importances": importances,
    }


def _predict_leg(fit: Dict[str, Any], df: pd.DataFrame, *, features: Sequence[str]) -> np.ndarray:
    X = _select_features(df, features).fillna(fit["medians"])
    return np.asarray(fit["model"].predict(X), dtype=float)


# ── Per-window evaluation ────────────────────────────────────────────────────

def evaluate_window(
    df: pd.DataFrame, window: WalkForwardWindow, *, n_trials: int,
    inner_valid_frac: float = DEFAULT_INNER_VALID_FRAC, min_test_rows: int = DEFAULT_MIN_TEST_ROWS,
) -> Dict[str, Any]:
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
    result["inner_train_end"] = inner_train_end
    result["inner_valid_start"] = inner_valid_start

    call_fit = _fit_leg_model(inner_train_df, inner_valid_df, features=CALL_FEATURES, target_col="net_ce_return", n_trials=n_trials)
    put_fit = _fit_leg_model(inner_train_df, inner_valid_df, features=PUT_FEATURES, target_col="net_pe_return", n_trials=n_trials)
    if call_fit is None or put_fit is None:
        result["status"] = "degenerate"
        result["reason"] = (
            f"not enough usable rows to fit one or both leg models "
            f"(call_fit={'ok' if call_fit else 'FAILED'}, put_fit={'ok' if put_fit else 'FAILED'})"
        )
        return result
    result["status"] = "ok"

    call_pred = _predict_leg(call_fit, test_df, features=CALL_FEATURES)
    put_pred = _predict_leg(put_fit, test_df, features=PUT_FEATURES)
    predicted_margin = compute_predicted_margin(call_pred, put_pred)
    net_ce = pd.to_numeric(test_df["net_ce_return"], errors="coerce").to_numpy()
    net_pe = pd.to_numeric(test_df["net_pe_return"], errors="coerce").to_numpy()
    actual_margin = compute_actual_margin(net_ce, net_pe)

    result["call_model"] = {k: v for k, v in call_fit.items() if k not in ("model", "medians")}
    result["put_model"] = {k: v for k, v in put_fit.items() if k not in ("model", "medians")}
    result["call_leg_eval"] = _regression_eval(call_pred, net_ce)
    result["put_leg_eval"] = _regression_eval(put_pred, net_pe)
    result["margin_eval"] = _regression_eval(predicted_margin, actual_margin)
    result["n_both_legs_test"] = int((~np.isnan(actual_margin)).sum())

    predicted_side = margin_to_side(predicted_margin)
    truth_side = test_df["payoff_winning_side"]
    result["binary_eval"] = evaluate_signal_accuracy(predicted_side, truth_side)

    # Kept for pooling (stripped before JSON serialization).
    result["_test_df"] = test_df
    result["_predicted_margin"] = predicted_margin
    result["_actual_margin"] = actual_margin
    result["_predicted_side"] = predicted_side
    result["_truth_side"] = truth_side
    result["_call_pred"] = call_pred
    result["_put_pred"] = put_pred
    result["_net_ce"] = net_ce
    result["_net_pe"] = net_pe
    return result


def pool_window_results(window_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [w for w in window_results if w.get("status") == "ok"]
    if not ok:
        return {"n_windows_ok": 0}
    predicted_margin = np.concatenate([w["_predicted_margin"] for w in ok])
    actual_margin = np.concatenate([w["_actual_margin"] for w in ok])
    call_pred = np.concatenate([w["_call_pred"] for w in ok])
    put_pred = np.concatenate([w["_put_pred"] for w in ok])
    net_ce = np.concatenate([w["_net_ce"] for w in ok])
    net_pe = np.concatenate([w["_net_pe"] for w in ok])
    predicted_side = pd.concat([w["_predicted_side"] for w in ok], ignore_index=True)
    truth_side = pd.concat([w["_truth_side"] for w in ok], ignore_index=True)
    return {
        "n_windows_ok": len(ok),
        "margin_eval": _regression_eval(predicted_margin, actual_margin),
        "call_leg_eval": _regression_eval(call_pred, net_ce),
        "put_leg_eval": _regression_eval(put_pred, net_pe),
        "binary_eval": evaluate_signal_accuracy(predicted_side, truth_side),
        "_predicted_margin": predicted_margin,
        "_predicted_side": predicted_side,
    }


# ── HEADLINE: independent per-leg ship-gate verdict ─────────────────────────

def leg_verdict(pooled_leg_eval: Dict[str, Any], per_window_leg_evals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Standalone verdict for ONE leg's regressor, entirely on its own terms
    -- reuses `train_entry_option_payoff_v1.compute_ship_gates` VERBATIM
    (same thresholds the validated entry-timing model itself had to clear:
    holdout_rows>=200, top_decile_mean_payoff>0, spearman_rho>=0.05,
    spearman_p_value<=0.05) against this leg's POOLED regression eval, plus
    a walk-forward-consistency check (fraction of individually-OK windows
    where this leg's OWN Spearman rho was positive) -- a pooled number
    driven by one outlier window is not the same claim as a consistently
    positive signal. `run_stress_test` is the gate the caller should use to
    decide whether this leg's delayed-action stress test is warranted."""
    gate_input = {
        "rows": pooled_leg_eval.get("rows", 0),
        "top_decile_mean_payoff": pooled_leg_eval.get("top_decile_mean"),
        "spearman_rho": pooled_leg_eval.get("spearman_rho"),
        "spearman_p_value": pooled_leg_eval.get("spearman_p_value"),
    }
    gates = compute_ship_gates(gate_input)
    all_pass = all(gates.values())
    rhos = [e["spearman_rho"] for e in per_window_leg_evals if e.get("spearman_rho") is not None]
    frac_positive = (sum(1 for r in rhos if r > 0) / len(rhos)) if rhos else 0.0
    return {
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "per_window_spearman_rho": rhos,
        "frac_windows_with_positive_rho": round(frac_positive, 4),
        "run_stress_test": all_pass,
    }


def run_leg_stress_test(
    ok_windows: Sequence[Dict[str, Any]],
    parquet_base: str,
    *,
    leg: str,
    pred_key: str,
    horizon_bars: int,
    lot_size: int,
    top_decile_frac: float = 0.10,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    """The MANDATORY delayed-action stress test for ONE leg's model, entirely
    standalone -- mirrors `run_repricing_wall_control.py`'s exact convention
    (top-`top_decile_frac` predicted-value fires, re-priced at +1/+2 bars
    holding the SAME original exit bar via
    `entry_quality_direction.compute_payoff_at_delay`, scored with
    `validation.check_repricing_wall`) but pools fires across all walk-
    forward OK windows instead of a single fixed holdout, and re-prices ONLY
    the leg this model actually predicts (`leg="CE"` -> `bp.ce.net_return`,
    `leg="PE"` -> `bp.pe.net_return`, via
    `breakout_agreement_direction.leg_return_for_side`) -- there is no
    "called side" choice here, this model only ever concerns itself with
    its own leg."""
    frames = []
    preds = []
    for w in ok_windows:
        frames.append(w["_test_df"][["trade_date", "bar_index"]])
        preds.append(w[pred_key])
    all_rows = pd.concat(frames, ignore_index=True)
    all_pred = np.concatenate(preds)
    cutoff = float(np.quantile(all_pred, 1.0 - top_decile_frac))
    mask = all_pred >= cutoff
    fired_rows = all_rows.loc[mask].reset_index(drop=True)
    n = len(fired_rows)
    log.info("leg=%s: top-%.0f%% predicted-%s cutoff=%.5f -> %d fired bars (pooled across OK windows)",
             leg, top_decile_frac * 100, pred_key.strip("_"), cutoff, n)

    out_return: Dict[int, List[Optional[float]]] = {0: [None] * n, 1: [None] * n, 2: [None] * n}
    by_date: Dict[str, List[int]] = {}
    for i, d in enumerate(fired_rows["trade_date"].astype(str)):
        by_date.setdefault(d, []).append(i)

    for trade_date, idxs in by_date.items():
        snaps = read_day_snapshots(parquet_base, trade_date)
        if not snaps:
            continue
        for i in idxs:
            bar_index = int(fired_rows.loc[i, "bar_index"])
            for delay in (0, 1, 2):
                bp = compute_payoff_at_delay(snaps, bar_index, horizon_bars=horizon_bars, delay_bars=delay, lot_size=lot_size)
                if bp is None:
                    continue
                out_return[delay][i] = leg_return_for_side(bp, leg)

    report = check_repricing_wall(
        payoff_at_fire=out_return[0], payoff_delay_1=out_return[1], payoff_delay_2=out_return[2],
        min_paired_bars=min_paired_bars, raise_on_fail=False,
    )
    return {"leg": leg, "n_fired": n, "top_decile_frac": top_decile_frac, "cutoff": cutoff, **asdict(report)}


# ── SECONDARY: margin-framed delayed-action stress test (duplicated glue, mirrors study_crossover_direction.py) ─

def run_delayed_stress_test(
    ok_windows: Sequence[Dict[str, Any]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    confident_frac: float,
    edge_threshold: float,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    frames = []
    margins = []
    for w in ok_windows:
        frames.append(w["_test_df"][["trade_date", "bar_index"]])
        margins.append(w["_predicted_margin"])
    all_rows = pd.concat(frames, ignore_index=True)
    all_margin = np.concatenate(margins)
    mask = select_confident_subset_mask(all_margin, frac=confident_frac)
    fired_rows = all_rows.loc[mask].reset_index(drop=True)
    fired_margin = all_margin[mask]
    fired_side = list(margin_to_side(fired_margin))

    n = len(fired_rows)
    out_side: Dict[int, List[Optional[str]]] = {0: [None] * n, 1: [None] * n, 2: [None] * n}
    out_return: Dict[int, List[Optional[float]]] = {0: [None] * n, 1: [None] * n, 2: [None] * n}
    by_date: Dict[str, List[int]] = {}
    for i, d in enumerate(fired_rows["trade_date"].astype(str)):
        by_date.setdefault(d, []).append(i)

    for trade_date, idxs in by_date.items():
        snaps = read_day_snapshots(parquet_base, trade_date)
        if not snaps:
            continue
        for i in idxs:
            bar_index = int(fired_rows.loc[i, "bar_index"])
            side = fired_side[i]
            for delay in (0, 1, 2):
                bp = compute_payoff_at_delay(snaps, bar_index, horizon_bars=horizon_bars, delay_bars=delay, lot_size=lot_size)
                if bp is None:
                    continue
                out_side[delay][i] = bp.winning_side
                out_return[delay][i] = leg_return_for_side(bp, side)

    direction_report = check_direction_repricing_wall(
        votes_at_fire=fired_side, truth_at_fire=out_side[0],
        votes_delay_1=fired_side, truth_delay_1=out_side[1],
        votes_delay_2=fired_side, truth_delay_2=out_side[2],
        min_paired_bars=min_paired_bars, edge_threshold=edge_threshold, raise_on_fail=False,
    )
    payoff_report = check_repricing_wall(
        payoff_at_fire=out_return[0], payoff_delay_1=out_return[1], payoff_delay_2=out_return[2],
        min_paired_bars=min_paired_bars, raise_on_fail=False,
    )
    return {
        "n_confident_bars": n,
        "direction_flip_test": asdict(direction_report),
        "called_leg_payoff_test": asdict(payoff_report),
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items() if not k.startswith("_") and k != "model" and k != "medians"}
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
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=".run/training_view/training_view_nifty_direction_margin.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--inner-valid-frac", type=float, default=DEFAULT_INNER_VALID_FRAC)
    ap.add_argument("--n-trials", type=int, default=20)
    ap.add_argument("--confident-frac", type=float, default=DEFAULT_CONFIDENT_FRAC)
    ap.add_argument("--stress-test-edge-threshold", type=float, default=DEFAULT_EDGE_THRESHOLD)
    ap.add_argument("--leg-top-decile-frac", type=float, default=0.10, help="Top-decile-fires fraction for EACH leg's own standalone stress test")
    ap.add_argument("--skip-stress-test", action="store_true", help="Skip BOTH the per-leg and the secondary margin stress tests")
    ap.add_argument("--force-stress-test", action="store_true", help="Run every stress test even if its own gate says no")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    log.info("loading %s", args.training_view_csv)
    df = pd.read_csv(args.training_view_csv)
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info(
        "loaded %d rows, %d trade_dates (%s..%s)",
        len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max(),
    )

    windows = build_expanding_windows(
        sorted(df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days,
    )
    log.info("built %d walk-forward windows", len(windows))

    window_results: List[Dict[str, Any]] = []
    for w in windows:
        log.info("=== window %d: train <= %s, test %s..%s ===", w.window_id, w.train_end, w.test_start, w.test_end)
        res = evaluate_window(df, w, n_trials=args.n_trials, inner_valid_frac=args.inner_valid_frac)
        log.info("window %d status=%s", w.window_id, res.get("status"))
        if res.get("status") == "ok":
            log.info(
                "  margin: rho=%s (p=%s) | binary acc=%s (n=%s, p=%s)",
                res["margin_eval"]["spearman_rho"], res["margin_eval"]["spearman_p_value"],
                res["binary_eval"]["accuracy"], res["binary_eval"]["n_voted"], res["binary_eval"]["permutation_p_value"],
            )
        window_results.append(res)

    pooled = pool_window_results(window_results)
    ok_windows = [w for w in window_results if w.get("status") == "ok"]

    # ── HEADLINE: two independent per-leg verdicts ──────────────────────────
    call_verdict: Dict[str, Any] = {}
    put_verdict: Dict[str, Any] = {}
    call_stress: Optional[Dict[str, Any]] = None
    put_stress: Optional[Dict[str, Any]] = None
    if pooled.get("n_windows_ok"):
        call_verdict = leg_verdict(pooled["call_leg_eval"], [w["call_leg_eval"] for w in ok_windows])
        put_verdict = leg_verdict(pooled["put_leg_eval"], [w["put_leg_eval"] for w in ok_windows])
        log.info("CALL leg ship_gates_all_pass=%s frac_windows_positive_rho=%s", call_verdict["ship_gates_all_pass"], call_verdict["frac_windows_with_positive_rho"])
        log.info("PUT  leg ship_gates_all_pass=%s frac_windows_positive_rho=%s", put_verdict["ship_gates_all_pass"], put_verdict["frac_windows_with_positive_rho"])

        if not args.skip_stress_test and (call_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY standalone delayed-action stress test for the CALL model (net_ce_return only)...")
            call_stress = run_leg_stress_test(
                ok_windows, args.parquet_base, leg="CE", pred_key="_call_pred",
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.leg_top_decile_frac,
            )
        if not args.skip_stress_test and (put_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY standalone delayed-action stress test for the PUT model (net_pe_return only)...")
            put_stress = run_leg_stress_test(
                ok_windows, args.parquet_base, leg="PE", pred_key="_put_pred",
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.leg_top_decile_frac,
            )
    else:
        log.info("no OK windows -- skipping both per-leg verdicts and stress tests")

    # ── SECONDARY: margin/cross-leg comparison (kept, never the headline) ──
    per_window_acc = [w["binary_eval"]["accuracy"] for w in ok_windows]
    margin_eval = pooled.get("margin_eval", {})
    binary_eval = pooled.get("binary_eval", {})
    margin_verdict = is_promising(
        pooled_margin_spearman_rho=margin_eval.get("spearman_rho"),
        pooled_margin_spearman_p=margin_eval.get("spearman_p_value"),
        pooled_top_decile_mean_actual_margin=margin_eval.get("top_decile_mean"),
        pooled_bottom_decile_mean_actual_margin=margin_eval.get("bottom_decile_mean"),
        pooled_binary_accuracy=binary_eval.get("accuracy"),
        pooled_binary_permutation_p=binary_eval.get("permutation_p_value"),
        per_window_binary_accuracy=per_window_acc,
    )
    log.info("[secondary] margin promising verdict: %s -- %s", margin_verdict.promising, margin_verdict.reasons or "no criterion cleared")

    margin_stress_test: Optional[Dict[str, Any]] = None
    if (margin_verdict.promising or args.force_stress_test) and not args.skip_stress_test and ok_windows:
        log.info("[secondary] running margin-framed delayed-action stress test on the pooled confident-calls subset...")
        margin_stress_test = run_delayed_stress_test(
            ok_windows, args.parquet_base, horizon_bars=args.horizon_bars, lot_size=args.lot_size,
            confident_frac=args.confident_frac, edge_threshold=args.stress_test_edge_threshold,
        )

    report = {
        "config": {
            "training_view_csv": args.training_view_csv, "lot_size": args.lot_size, "horizon_bars": args.horizon_bars,
            "n_windows": args.n_windows, "min_train_days": args.min_train_days, "inner_valid_frac": args.inner_valid_frac,
            "n_trials": args.n_trials, "confident_frac": args.confident_frac, "leg_top_decile_frac": args.leg_top_decile_frac,
            "call_features": CALL_FEATURES, "put_features": PUT_FEATURES,
        },
        "windows": _json_safe(window_results),
        "pooled": _json_safe(pooled),
        "headline": {
            "call_leg_verdict": _json_safe(call_verdict),
            "put_leg_verdict": _json_safe(put_verdict),
            "call_leg_stress_test": _json_safe(call_stress) if call_stress is not None else None,
            "put_leg_stress_test": _json_safe(put_stress) if put_stress is not None else None,
        },
        "secondary_margin_section": {
            "promising_verdict": asdict(margin_verdict),
            "stress_test": _json_safe(margin_stress_test) if margin_stress_test is not None else None,
        },
    }

    print("\n=== Per-window results (both legs, standalone) ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: train<={w.get('train_end')}, test {w['test_start']}..{w['test_end']} (n_test={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')} reason={w.get('reason', '')}")
            continue
        c = w["call_leg_eval"]
        p = w["put_leg_eval"]
        print(f"  CALL (net_ce_return): rho={c['spearman_rho']} (p={c['spearman_p_value']}, n={c['rows']}) top_decile={c['top_decile_mean']} bottom_decile={c['bottom_decile_mean']}")
        print(f"  PUT  (net_pe_return): rho={p['spearman_rho']} (p={p['spearman_p_value']}, n={p['rows']}) top_decile={p['top_decile_mean']} bottom_decile={p['bottom_decile_mean']}")

    print("\n" + "=" * 78)
    print("=== HEADLINE: independent per-leg standalone verdicts (pooled) ===")
    print("=" * 78)
    if pooled.get("n_windows_ok"):
        c = pooled["call_leg_eval"]
        p = pooled["put_leg_eval"]
        print(f"\n--- CALL model (net_ce_return), n_windows_ok={pooled['n_windows_ok']} ---")
        print(f"  rho={c['spearman_rho']} (p={c['spearman_p_value']}, n={c['rows']}) top_decile_mean={c['top_decile_mean']} bottom_decile_mean={c['bottom_decile_mean']}")
        print(f"  ship_gates={call_verdict.get('ship_gates')} -> ALL_PASS={call_verdict.get('ship_gates_all_pass')}")
        print(f"  per-window rho={call_verdict.get('per_window_spearman_rho')} (frac windows with positive rho={call_verdict.get('frac_windows_with_positive_rho')})")
        if call_stress:
            r = call_stress
            print(f"  STRESS TEST (top {r['top_decile_frac']:.0%} fires, n_fired={r['n_fired']}, n_paired={r['n_paired_bars']}): "
                  f"mean@fire={r.get('mean_payoff_at_fire')} mean@+1={r.get('mean_payoff_delay_1')} mean@+2={r.get('mean_payoff_delay_2')} "
                  f"ok={r['ok']} -- {r.get('reason')}")
        else:
            print("  STRESS TEST: not run (ship gates did not clear)")

        print(f"\n--- PUT model (net_pe_return), n_windows_ok={pooled['n_windows_ok']} ---")
        print(f"  rho={p['spearman_rho']} (p={p['spearman_p_value']}, n={p['rows']}) top_decile_mean={p['top_decile_mean']} bottom_decile_mean={p['bottom_decile_mean']}")
        print(f"  ship_gates={put_verdict.get('ship_gates')} -> ALL_PASS={put_verdict.get('ship_gates_all_pass')}")
        print(f"  per-window rho={put_verdict.get('per_window_spearman_rho')} (frac windows with positive rho={put_verdict.get('frac_windows_with_positive_rho')})")
        if put_stress:
            r = put_stress
            print(f"  STRESS TEST (top {r['top_decile_frac']:.0%} fires, n_fired={r['n_fired']}, n_paired={r['n_paired_bars']}): "
                  f"mean@fire={r.get('mean_payoff_at_fire')} mean@+1={r.get('mean_payoff_delay_1')} mean@+2={r.get('mean_payoff_delay_2')} "
                  f"ok={r['ok']} -- {r.get('reason')}")
        else:
            print("  STRESS TEST: not run (ship gates did not clear)")
    else:
        print("  no OK windows")

    print("\n" + "=" * 78)
    print("=== SECONDARY (NOT the headline): margin / cross-leg comparison ===")
    print("=" * 78)
    if pooled.get("n_windows_ok"):
        m = margin_eval
        b = binary_eval
        print(f"  margin:  rho={m.get('spearman_rho')} (p={m.get('spearman_p_value')}, n={m.get('rows')}) top_decile={m.get('top_decile_mean')} bottom_decile={m.get('bottom_decile_mean')}")
        print(f"  binary:  coverage={b.get('coverage')} n={b.get('n_voted')} accuracy={b.get('accuracy')} 95%CI=[{b.get('bootstrap_ci_low')}, {b.get('bootstrap_ci_high')}] perm_p={b.get('permutation_p_value')}")
        print(f"  promising: {margin_verdict.promising} -- {margin_verdict.reasons or ['no criterion cleared']}")
        if margin_stress_test:
            d = margin_stress_test["direction_flip_test"]
            pf = margin_stress_test["called_leg_payoff_test"]
            print(f"  margin stress test (n_confident_bars={margin_stress_test['n_confident_bars']}):")
            print(f"    direction: ok={d['ok']} n={d['n_paired_bars']} acc_fire={d.get('accuracy_at_fire')} acc_d1={d.get('accuracy_delay_1')} acc_d2={d.get('accuracy_delay_2')} -- {d.get('reason')}")
            print(f"    payoff:    ok={pf['ok']} n={pf['n_paired_bars']} mean_fire={pf.get('mean_payoff_at_fire')} mean_d1={pf.get('mean_payoff_delay_1')} mean_d2={pf.get('mean_payoff_delay_2')} -- {pf.get('reason')}")
    else:
        print("  no OK windows")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
