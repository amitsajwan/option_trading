"""ONE joint multiclass direction model (NIFTY, 2026-09-27) -- sixth
direction-research track today. Trains a SINGLE XGBoost multiclass
classifier (``objective="multi:softprob"``, 3 classes: NO_TRADE / CALL /
PUT) per walk-forward window, instead of track 5's two INDEPENDENT
regressors (``train_direction_margin_v1.py``). This is the closest
structural/methodological template for this script (same walk-forward
orchestration shape, same reporting conventions) -- read its module
docstring first for the full lineage of today's five prior tracks.

THE testable, architecturally-motivated difference from track 5: a joint
multiclass classifier allocates probability mass across NO_TRADE/CALL/PUT
from SHARED tree splits, so it can in principle learn "this feature
combination means confidently CALL, as distinct from either NO_TRADE or
PUT" as one joint pattern -- something two independently-trained regressors
(each seeing only its own leg's target, with no visibility into the other
leg or into "should we trade at all") structurally cannot represent. Track
5's own finding: the call regressor has real, walk-forward-consistent
DEFENSIVE ranking skill (pooled Spearman rho=0.076, positive in 5/5
windows) but its own top decile was still net-negative (-0.55%, see
``.run/direction_margin/nifty_direction_margin_FINAL.json``'s
``pooled.call_leg_eval.top_decile_mean`` = -0.00547); the put regressor
showed no real independent skill (rho=0.014, inconsistent sign). The single
most important question this script answers: does training this as ONE
joint decision manage to find a genuinely PROFITABLE high-confidence
CALL or PUT subset that the two independent models missed -- not just a
defensive/avoid-only signal, and not a worse version of the same thing
diluted by a 3-way split.

Import-isolation constraint (matches every sibling direction-research
module): must NEVER import ``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime``. Registry-recorded
research only.

Methodology:
  1. Load ``training_view_nifty_direction_margin.csv.gz`` (built by track
     5's own ``build_direction_margin_training_view.py`` -- reused
     directly, no new training-view build needed; it already carries
     ENTRY_FEATURES_V3 + ``net_ce_return``/``net_pe_return`` + the ten
     per-leg raw LEVEL features).
  2. Derive the 3-class label from ``net_ce_return``/``net_pe_return`` via
     ``joint_direction.compute_three_class_label`` -- see that module's
     docstring for the exact rule, tie-break convention, and the real
     2026-09-27 class-balance measurement (CALL=41.9%, PUT=41.3%,
     NO_TRADE=16.8%, confirming the task's expectation that NO_TRADE is
     the rarer, more informative class here).
  3. Split into the SAME 5 expanding-origin walk-forward windows every
     sibling track today used (``conditional_direction
     .build_expanding_windows``, ``min_train_days=200``) for direct
     comparability. Per window: carve the window's own TRAIN date range
     into an inner-train/inner-valid split
     (``direction_margin.carve_inner_validation``, reused verbatim), run
     ONE Optuna HPO for the joint multiclass model (features:
     ENTRY_FEATURES_V3 + CALL_ONLY_FEATURES + PUT_ONLY_FEATURES -- the
     model needs to see BOTH legs' features to make a 3-way comparison,
     unlike track 5's leg models which each only saw their own leg's
     extras), optimizing validation multiclass log-loss (see
     ``run_multiclass_hpo``'s docstring for why log-loss, not accuracy or
     macro AUC, is the HPO objective here).
  4. STANDARD diagnostics on the window's test slice: confusion matrix,
     per-class precision/recall/F1, macro/weighted F1
     (``joint_direction.multiclass_diagnostics``).
  5. DECISION-RELEVANT evaluation (the headline): for each class, restrict
     to bars where that class is the model's own argmax, then decile-lift
     P(class) against the class's real economic outcome
     (``joint_direction.class_decile_eval``, itself reusing
     ``train_entry_option_payoff_v1.decile_lift_table`` verbatim) -- CALL
     against ``net_ce_return``, PUT against ``net_pe_return``, NO_TRADE
     against ``max(net_ce_return, net_pe_return)`` (does confidently
     calling NO_TRADE actually correlate with a LOW best-achievable
     return, or is it just noise wearing a confident label).
  6. Per-class ship-gate verdict (``class_verdict``) -- CALL/PUT reuse
     ``train_entry_option_payoff_v1.compute_ship_gates`` verbatim (same
     thresholds track 5's own leg models had to clear); NO_TRADE uses the
     sign-flipped ``joint_direction.compute_no_trade_ship_gates`` (its
     "working" direction is a NEGATIVE spearman rho, not positive). A
     class that clears its own ship gates gets the MANDATORY delayed-
     action stress test on ITS OWN argmax-conditioned top-decile fires,
     re-pricing ONLY the leg it actually calls (``run_class_stress_test``,
     CALL -> CE, PUT -> PE) -- NO_TRADE has no leg to re-price and is
     excluded from this gate by construction.
  7. THE CRITICAL COMPARISON (``joint_direction.compare_top_decile_to_baseline``):
     does the joint model's CALL top-decile mean beat track 5's
     independent call regressor's own top-decile mean (-0.00547, the exact
     pooled number from ``nifty_direction_margin_FINAL.json``) -- and does
     it cross into genuinely positive territory, not just "less negative"?
     Same comparison run for PUT against track 5's put regressor
     (-0.00434).

Usage:
    python -m ml_pipeline_2.scripts.train_joint_direction_v1 \\
        --training-view-csv .run/training_view/training_view_nifty_direction_margin.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 --n-trials 20 \\
        --output-json .run/joint_direction/nifty_joint_direction_report.json
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
log = logging.getLogger("train_joint_direction_v1")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.breakout_agreement_direction import leg_return_for_side  # noqa: E402
from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
)
from ml_pipeline_2.pipeline.direction_margin import (  # noqa: E402
    CALL_ONLY_FEATURES,
    PUT_ONLY_FEATURES,
    carve_inner_validation,
)
from ml_pipeline_2.pipeline.entry_quality_direction import compute_payoff_at_delay  # noqa: E402
from ml_pipeline_2.pipeline.joint_direction import (  # noqa: E402
    CALL,
    CLASS_NAMES,
    CLASS_TO_INT,
    NO_TRADE,
    PUT,
    argmax_class,
    class_actual_value,
    class_balance,
    class_decile_eval,
    compare_top_decile_to_baseline,
    compute_no_trade_ship_gates,
    compute_three_class_label,
    encode_label,
    multiclass_diagnostics,
    top_decile_mask_within_argmax,
)
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402
from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import compute_ship_gates  # noqa: E402

JOINT_FEATURES: List[str] = list(ENTRY_FEATURES_V3) + list(CALL_ONLY_FEATURES) + list(PUT_ONLY_FEATURES)

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_INNER_VALID_FRAC = 0.15
DEFAULT_MIN_TEST_ROWS = 30
DEFAULT_MIN_FIT_ROWS = 200
DEFAULT_TOP_DECILE_FRAC = 0.10

# Track 5's own pooled, real, documented top-decile numbers -- see
# `.run/direction_margin/nifty_direction_margin_FINAL.json`'s
# `pooled.call_leg_eval.top_decile_mean` / `pooled.put_leg_eval.top_decile_mean`,
# confirmed 2026-09-27. The exact baseline THE CRITICAL COMPARISON (item 4
# of the owning task) is run against.
TRACK5_CALL_TOP_DECILE_BASELINE = -0.00547
TRACK5_PUT_TOP_DECILE_BASELINE = -0.00434


def _select_features(df: pd.DataFrame, features: Sequence[str]) -> pd.DataFrame:
    missing = [f for f in features if f not in df.columns]
    if missing:
        raise ValueError(f"training view is missing expected feature columns: {missing}")
    return df[list(features)].apply(pd.to_numeric, errors="coerce")


def run_multiclass_hpo(
    X_train: pd.DataFrame, y_train_int: np.ndarray,
    X_valid: pd.DataFrame, y_valid_int: np.ndarray,
    n_trials: int = 20,
) -> Tuple[Any, Dict[str, Any], float, Optional[float]]:
    """Optuna HPO over XGBClassifier(objective="multi:softprob", num_class=3)
    hyperparameters, optimizing validation MULTICLASS LOG-LOSS (minimized).

    Why log-loss, not accuracy or macro one-vs-rest AUC (the two
    alternatives the owning task explicitly floated): (1) log-loss is the
    direct held-out counterpart of what `multi:softprob` itself minimizes
    at TRAINING time (softmax cross-entropy) -- tuning hyperparameters
    against the same family of objective the model is fit with avoids the
    classic train-objective/validation-metric mismatch this pipeline's own
    convention warns about (`docs/MODEL_STUDY_CHECKLIST.md`: "a model's
    best validation score can still be its worst holdout score" --
    NIFTY's 0.25%-target model scored best on validation, worst on true
    holdout, precisely because its selection metric didn't match what it
    was fit to do). (2) Log-loss rewards well-CALIBRATED, magnitude-graded
    probabilities across all three classes jointly -- exactly the property
    this study's headline evaluation depends on: `class_decile_eval` sorts
    bars by raw P(class) into deciles, which is only a meaningful ranking
    if the probability column itself carries real graded information, not
    just "which side of 1/3 is it on." Plain accuracy is blind to that --
    a model with P(CALL) always either 0.34 or 0.90 (same argmax decision,
    wildly different discriminative power for the decile test) scores
    identically on accuracy but very differently on log-loss. Macro
    one-vs-rest AUC would also reward ranking quality, but only per-class
    in isolation, never the JOINT three-way calibration that log-loss
    checks (a model that's well-ranked on each one-vs-rest split can still
    assign badly-scaled absolute probabilities across classes, e.g.
    P(NO_TRADE)+P(CALL)+P(PUT) summing correctly but everything shifted
    toward the majority classes) -- log-loss is the stricter, more
    complete check and is reported here as the optimization target.
    Macro OvR AUC on the SAME validation fold is still computed and
    returned as a secondary, non-optimized diagnostic for transparency
    (this is the alternative the task asked to justify NOT picking).
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        import xgboost as xgb
        from sklearn.metrics import log_loss, roc_auc_score
    except ImportError as exc:
        raise ImportError(f"Install optuna + xgboost + scikit-learn: pip install optuna xgboost scikit-learn — {exc}")

    n_classes = len(CLASS_NAMES)
    all_class_ints = list(range(n_classes))

    def objective(trial):
        params = {
            "n_estimators":     trial.suggest_int("n_estimators", 200, 1000),
            "max_depth":        trial.suggest_int("max_depth", 3, 8),
            "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":        trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
            "reg_alpha":        trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda":       trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
            "objective":        "multi:softprob",
            "num_class":        n_classes,
            "random_state":     42,
            "n_jobs":           -1,
        }
        model = xgb.XGBClassifier(**params)
        model.fit(X_train, y_train_int, verbose=False)
        proba = model.predict_proba(X_valid)
        ll = log_loss(y_valid_int, proba, labels=all_class_ints)
        return float(ll)

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    log.info("Best validation log-loss=%.4f params=%s", study.best_value, study.best_params)

    best = dict(study.best_params)
    best["objective"] = "multi:softprob"
    best["num_class"] = n_classes
    best["random_state"] = 42
    best["n_jobs"] = -1
    final = xgb.XGBClassifier(**best)
    final.fit(X_train, y_train_int, verbose=False)

    valid_proba = final.predict_proba(X_valid)
    try:
        macro_auc_ovr = float(
            roc_auc_score(y_valid_int, valid_proba, multi_class="ovr", average="macro", labels=all_class_ints)
        )
    except ValueError:
        macro_auc_ovr = None  # e.g. a class entirely absent from this validation slice

    return final, study.best_params, float(study.best_value), macro_auc_ovr


def _fit_joint_model(
    train_df: pd.DataFrame, valid_df: pd.DataFrame, *, features: Sequence[str], n_trials: int,
) -> Optional[Dict[str, Any]]:
    """Fit the ONE joint multiclass model via HPO on its own (train, valid)
    slice, dropping rows where EITHER leg's net return is missing (so the
    3-class label itself is undefined) -- returns None (caller marks the
    window degenerate) if there aren't enough usable rows on either side."""
    tr_label = compute_three_class_label(train_df["net_ce_return"], train_df["net_pe_return"])
    va_label = compute_three_class_label(valid_df["net_ce_return"], valid_df["net_pe_return"])
    tr_mask = tr_label.notna().to_numpy()
    va_mask = va_label.notna().to_numpy()
    tr = train_df[tr_mask].reset_index(drop=True)
    va = valid_df[va_mask].reset_index(drop=True)
    tr_label = tr_label[tr_mask].reset_index(drop=True)
    va_label = va_label[va_mask].reset_index(drop=True)
    if len(tr) < DEFAULT_MIN_FIT_ROWS or len(va) < max(20, int(DEFAULT_MIN_FIT_ROWS * 0.1)):
        return None

    X_train = _select_features(tr, features)
    X_valid = _select_features(va, features)
    y_train_int = encode_label(tr_label.to_numpy())
    y_valid_int = encode_label(va_label.to_numpy())

    medians = X_train.median(numeric_only=True)
    X_train = X_train.fillna(medians)
    X_valid = X_valid.fillna(medians)

    model, best_params, best_valid_logloss, best_valid_macro_auc = run_multiclass_hpo(
        X_train, y_train_int, X_valid, y_valid_int, n_trials=n_trials,
    )
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
        "best_params": best_params,
        "best_valid_logloss": round(best_valid_logloss, 4),
        "best_valid_macro_auc_ovr": round(best_valid_macro_auc, 4) if best_valid_macro_auc is not None else None,
        "train_class_balance": class_balance(tr_label.to_numpy()),
        "valid_class_balance": class_balance(va_label.to_numpy()),
        "feature_importances": importances,
    }


def _predict_proba(fit: Dict[str, Any], df: pd.DataFrame, *, features: Sequence[str]) -> np.ndarray:
    X = _select_features(df, features).fillna(fit["medians"])
    return np.asarray(fit["model"].predict_proba(X), dtype=float)


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

    fit = _fit_joint_model(inner_train_df, inner_valid_df, features=JOINT_FEATURES, n_trials=n_trials)
    if fit is None:
        result["status"] = "degenerate"
        result["reason"] = "not enough usable rows (both legs resolved) to fit the joint model on this window's inner train/valid split"
        return result
    result["status"] = "ok"
    result["model_meta"] = {k: v for k, v in fit.items() if k not in ("model", "medians")}

    test_label = compute_three_class_label(test_df["net_ce_return"], test_df["net_pe_return"])
    test_mask = test_label.notna().to_numpy()
    test_eval_df = test_df[test_mask].reset_index(drop=True)
    test_label = test_label[test_mask].reset_index(drop=True)
    result["n_test_resolved"] = int(len(test_eval_df))
    result["test_class_balance"] = class_balance(test_label.to_numpy())

    proba = _predict_proba(fit, test_eval_df, features=JOINT_FEATURES)
    pred_labels = argmax_class(proba)
    true_labels = test_label.to_numpy()
    net_ce = pd.to_numeric(test_eval_df["net_ce_return"], errors="coerce").to_numpy()
    net_pe = pd.to_numeric(test_eval_df["net_pe_return"], errors="coerce").to_numpy()

    result["diagnostics"] = multiclass_diagnostics(true_labels, pred_labels)

    call_actual = class_actual_value(net_ce, net_pe, target_class=CALL)
    put_actual = class_actual_value(net_ce, net_pe, target_class=PUT)
    no_trade_actual = class_actual_value(net_ce, net_pe, target_class=NO_TRADE)
    result["call_eval"] = class_decile_eval(proba[:, CLASS_TO_INT[CALL]], call_actual, pred_labels, target_class=CALL)
    result["put_eval"] = class_decile_eval(proba[:, CLASS_TO_INT[PUT]], put_actual, pred_labels, target_class=PUT)
    result["no_trade_eval"] = class_decile_eval(proba[:, CLASS_TO_INT[NO_TRADE]], no_trade_actual, pred_labels, target_class=NO_TRADE)

    # Kept for pooling (stripped before JSON serialization).
    result["_test_df"] = test_eval_df[["trade_date", "bar_index"]]
    result["_proba"] = proba
    result["_pred_labels"] = pred_labels
    result["_true_labels"] = true_labels
    result["_net_ce"] = net_ce
    result["_net_pe"] = net_pe
    return result


def pool_window_results(window_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [w for w in window_results if w.get("status") == "ok"]
    if not ok:
        return {"n_windows_ok": 0}
    proba = np.concatenate([w["_proba"] for w in ok], axis=0)
    pred_labels = np.concatenate([w["_pred_labels"] for w in ok])
    true_labels = np.concatenate([w["_true_labels"] for w in ok])
    net_ce = np.concatenate([w["_net_ce"] for w in ok])
    net_pe = np.concatenate([w["_net_pe"] for w in ok])
    test_df = pd.concat([w["_test_df"] for w in ok], ignore_index=True)

    call_actual = class_actual_value(net_ce, net_pe, target_class=CALL)
    put_actual = class_actual_value(net_ce, net_pe, target_class=PUT)
    no_trade_actual = class_actual_value(net_ce, net_pe, target_class=NO_TRADE)
    return {
        "n_windows_ok": len(ok),
        "diagnostics": multiclass_diagnostics(true_labels, pred_labels),
        "call_eval": class_decile_eval(proba[:, CLASS_TO_INT[CALL]], call_actual, pred_labels, target_class=CALL),
        "put_eval": class_decile_eval(proba[:, CLASS_TO_INT[PUT]], put_actual, pred_labels, target_class=PUT),
        "no_trade_eval": class_decile_eval(proba[:, CLASS_TO_INT[NO_TRADE]], no_trade_actual, pred_labels, target_class=NO_TRADE),
        "_proba": proba, "_pred_labels": pred_labels, "_test_df": test_df,
    }


# ── HEADLINE: per-class ship-gate verdicts ──────────────────────────────────

def class_verdict(pooled_class_eval: Dict[str, Any], per_window_class_evals: Sequence[Dict[str, Any]], *, class_name: str) -> Dict[str, Any]:
    """Standalone verdict for ONE class, on its own terms. CALL/PUT reuse
    `train_entry_option_payoff_v1.compute_ship_gates` VERBATIM (same
    thresholds track 5's own leg models had to clear: rows>=200,
    top_decile_mean_payoff>0, spearman_rho>=0.05, spearman_p_value<=0.05).
    NO_TRADE uses the sign-flipped `joint_direction.compute_no_trade_ship_gates`
    -- its own "walk-forward consistency" check therefore also flips sign
    (a NEGATIVE per-window rho is the GOOD direction for NO_TRADE)."""
    if class_name == NO_TRADE:
        gates = compute_no_trade_ship_gates(pooled_class_eval)
    else:
        gate_input = {
            "rows": pooled_class_eval.get("rows", 0),
            "top_decile_mean_payoff": pooled_class_eval.get("top_decile_mean"),
            "spearman_rho": pooled_class_eval.get("spearman_rho"),
            "spearman_p_value": pooled_class_eval.get("spearman_p_value"),
        }
        gates = compute_ship_gates(gate_input)
    all_pass = all(gates.values())
    rhos = [e["spearman_rho"] for e in per_window_class_evals if e.get("spearman_rho") is not None]
    if class_name == NO_TRADE:
        frac_consistent = (sum(1 for r in rhos if r < 0) / len(rhos)) if rhos else 0.0
    else:
        frac_consistent = (sum(1 for r in rhos if r > 0) / len(rhos)) if rhos else 0.0
    return {
        "class": class_name,
        "ship_gates": gates,
        "ship_gates_all_pass": all_pass,
        "per_window_spearman_rho": rhos,
        "frac_windows_consistent_sign": round(frac_consistent, 4),
        # NO_TRADE has no tradeable leg to re-price -- never gets the stress test.
        "run_stress_test": bool(all_pass and class_name != NO_TRADE),
    }


def run_class_stress_test(
    ok_windows: Sequence[Dict[str, Any]],
    parquet_base: str,
    *,
    target_class: str,
    horizon_bars: int,
    lot_size: int,
    top_decile_frac: float = DEFAULT_TOP_DECILE_FRAC,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    """The MANDATORY delayed-action stress test for one economically-actionable
    class (CALL or PUT), pooled across all walk-forward OK windows -- mirrors
    `train_direction_margin_v1.run_leg_stress_test`'s exact convention
    (re-price top-decile fires at +1/+2 bars holding the SAME original exit
    bar via `entry_quality_direction.compute_payoff_at_delay`, scored with
    `validation.check_repricing_wall`), but the "fired" set is selected via
    `joint_direction.top_decile_mask_within_argmax` -- ARGMAX-conditioned,
    unlike track 5's leg models (which had no argmax/other-class concept and
    ranked every test row by raw predicted value). This re-prices ONLY the
    leg `target_class` actually calls (CALL -> CE, PUT -> PE)."""
    if target_class not in (CALL, PUT):
        raise ValueError(f"run_class_stress_test: target_class must be CALL or PUT, got {target_class!r}")
    leg = "CE" if target_class == CALL else "PE"

    frames = []
    probas = []
    argmaxes = []
    for w in ok_windows:
        frames.append(w["_test_df"])
        probas.append(w["_proba"][:, CLASS_TO_INT[target_class]])
        argmaxes.append(w["_pred_labels"])
    all_rows = pd.concat(frames, ignore_index=True).reset_index(drop=True)
    all_proba = np.concatenate(probas)
    all_argmax = np.concatenate(argmaxes)
    mask = top_decile_mask_within_argmax(all_proba, all_argmax, target_class=target_class, frac=top_decile_frac)
    fired_rows = all_rows.loc[mask].reset_index(drop=True)
    n = len(fired_rows)
    log.info(
        "class=%s (leg=%s): top-%.0f%% of P(%s) among argmax==%s bars -> %d fired bars (pooled across OK windows)",
        target_class, leg, top_decile_frac * 100, target_class, target_class, n,
    )

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
    return {"target_class": target_class, "leg": leg, "n_fired": n, "top_decile_frac": top_decile_frac, **asdict(report)}


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
    ap.add_argument("--top-decile-frac", type=float, default=DEFAULT_TOP_DECILE_FRAC, help="Top-decile-fires fraction for EACH class's own standalone stress test")
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--force-stress-test", action="store_true", help="Run every class's stress test even if its own gate says no")
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
    overall_balance = class_balance(compute_three_class_label(df["net_ce_return"], df["net_pe_return"]).to_numpy())
    log.info("overall class balance: %s", overall_balance["fractions"])

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
            d = res["diagnostics"]
            log.info("  accuracy=%s macro_f1=%s weighted_f1=%s", d["accuracy"], d["macro_f1"], d["weighted_f1"])
            for cls, key in ((CALL, "call_eval"), (PUT, "put_eval"), (NO_TRADE, "no_trade_eval")):
                e = res[key]
                log.info("  %s: n_argmax=%s rho=%s top_decile=%s bottom_decile=%s", cls, e["n_argmax"], e["spearman_rho"], e["top_decile_mean"], e["bottom_decile_mean"])
        window_results.append(res)

    pooled = pool_window_results(window_results)
    ok_windows = [w for w in window_results if w.get("status") == "ok"]

    call_verdict: Dict[str, Any] = {}
    put_verdict: Dict[str, Any] = {}
    no_trade_verdict: Dict[str, Any] = {}
    call_stress: Optional[Dict[str, Any]] = None
    put_stress: Optional[Dict[str, Any]] = None
    if pooled.get("n_windows_ok"):
        call_verdict = class_verdict(pooled["call_eval"], [w["call_eval"] for w in ok_windows], class_name=CALL)
        put_verdict = class_verdict(pooled["put_eval"], [w["put_eval"] for w in ok_windows], class_name=PUT)
        no_trade_verdict = class_verdict(pooled["no_trade_eval"], [w["no_trade_eval"] for w in ok_windows], class_name=NO_TRADE)
        log.info("CALL     ship_gates_all_pass=%s frac_consistent=%s", call_verdict["ship_gates_all_pass"], call_verdict["frac_windows_consistent_sign"])
        log.info("PUT      ship_gates_all_pass=%s frac_consistent=%s", put_verdict["ship_gates_all_pass"], put_verdict["frac_windows_consistent_sign"])
        log.info("NO_TRADE ship_gates_all_pass=%s frac_consistent=%s", no_trade_verdict["ship_gates_all_pass"], no_trade_verdict["frac_windows_consistent_sign"])

        if not args.skip_stress_test and (call_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY delayed-action stress test for CALL...")
            call_stress = run_class_stress_test(
                ok_windows, args.parquet_base, target_class=CALL,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.top_decile_frac,
            )
        if not args.skip_stress_test and (put_verdict["run_stress_test"] or args.force_stress_test):
            log.info("running MANDATORY delayed-action stress test for PUT...")
            put_stress = run_class_stress_test(
                ok_windows, args.parquet_base, target_class=PUT,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size, top_decile_frac=args.top_decile_frac,
            )
    else:
        log.info("no OK windows -- skipping all class verdicts and stress tests")

    call_vs_track5 = compare_top_decile_to_baseline(
        pooled.get("call_eval", {}).get("top_decile_mean"), TRACK5_CALL_TOP_DECILE_BASELINE,
    )
    put_vs_track5 = compare_top_decile_to_baseline(
        pooled.get("put_eval", {}).get("top_decile_mean"), TRACK5_PUT_TOP_DECILE_BASELINE,
    )
    log.info("CRITICAL COMPARISON vs track 5: CALL %s | PUT %s", call_vs_track5, put_vs_track5)

    report = {
        "config": {
            "training_view_csv": args.training_view_csv, "lot_size": args.lot_size, "horizon_bars": args.horizon_bars,
            "n_windows": args.n_windows, "min_train_days": args.min_train_days, "inner_valid_frac": args.inner_valid_frac,
            "n_trials": args.n_trials, "top_decile_frac": args.top_decile_frac, "joint_features": JOINT_FEATURES,
            "track5_call_top_decile_baseline": TRACK5_CALL_TOP_DECILE_BASELINE,
            "track5_put_top_decile_baseline": TRACK5_PUT_TOP_DECILE_BASELINE,
        },
        "overall_class_balance": overall_balance,
        "windows": _json_safe(window_results),
        "pooled": _json_safe(pooled),
        "headline": {
            "call_verdict": _json_safe(call_verdict),
            "put_verdict": _json_safe(put_verdict),
            "no_trade_verdict": _json_safe(no_trade_verdict),
            "call_stress_test": _json_safe(call_stress) if call_stress is not None else None,
            "put_stress_test": _json_safe(put_stress) if put_stress is not None else None,
            "call_vs_track5_independent_regressor": call_vs_track5,
            "put_vs_track5_independent_regressor": put_vs_track5,
        },
    }

    print("\n=== Class balance (resolved rows, both legs usable) ===")
    print(overall_balance["fractions"])

    print("\n=== Per-window results ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: train<={w.get('train_end')}, test {w['test_start']}..{w['test_end']} (n_test={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')} reason={w.get('reason', '')}")
            continue
        d = w["diagnostics"]
        print(f"  accuracy={d['accuracy']} macro_f1={d['macro_f1']} weighted_f1={d['weighted_f1']}")
        print(f"  confusion_matrix (rows=true, cols=pred, order={d['labels']}): {d['confusion_matrix']}")
        for cls, key in ((CALL, "call_eval"), (PUT, "put_eval"), (NO_TRADE, "no_trade_eval")):
            e = w[key]
            print(f"  {cls}: n_argmax={e['n_argmax']} rho={e['spearman_rho']} (p={e['spearman_p_value']}) top_decile={e['top_decile_mean']} bottom_decile={e['bottom_decile_mean']}")

    print("\n" + "=" * 78)
    print("=== HEADLINE: per-class standalone verdicts (pooled) ===")
    print("=" * 78)
    if pooled.get("n_windows_ok"):
        d = pooled["diagnostics"]
        print(f"\nPooled standard diagnostics: accuracy={d['accuracy']} macro_f1={d['macro_f1']} weighted_f1={d['weighted_f1']}")
        print(f"confusion_matrix (rows=true, cols=pred, order={d['labels']}): {d['confusion_matrix']}")
        for cls, key, verdict, stress in (
            (CALL, "call_eval", call_verdict, call_stress),
            (PUT, "put_eval", put_verdict, put_stress),
            (NO_TRADE, "no_trade_eval", no_trade_verdict, None),
        ):
            e = pooled[key]
            print(f"\n--- {cls}, n_windows_ok={pooled['n_windows_ok']} ---")
            print(f"  n_argmax={e['n_argmax']} rho={e['spearman_rho']} (p={e['spearman_p_value']}) top_decile_mean={e['top_decile_mean']} bottom_decile_mean={e['bottom_decile_mean']}")
            print(f"  ship_gates={verdict.get('ship_gates')} -> ALL_PASS={verdict.get('ship_gates_all_pass')}")
            print(f"  per-window rho={verdict.get('per_window_spearman_rho')} (frac consistent sign={verdict.get('frac_windows_consistent_sign')})")
            if stress:
                print(f"  STRESS TEST (top {stress['top_decile_frac']:.0%} argmax-conditioned fires, n_fired={stress['n_fired']}, n_paired={stress['n_paired_bars']}): "
                      f"mean@fire={stress.get('mean_payoff_at_fire')} mean@+1={stress.get('mean_payoff_delay_1')} mean@+2={stress.get('mean_payoff_delay_2')} "
                      f"ok={stress['ok']} -- {stress.get('reason')}")
            elif cls != NO_TRADE:
                print("  STRESS TEST: not run (ship gates did not clear)")

        print("\n" + "=" * 78)
        print("=== THE CRITICAL COMPARISON vs track 5's independent regressors ===")
        print("=" * 78)
        print(f"  CALL: joint={call_vs_track5['joint_top_decile_mean']} vs track5={call_vs_track5['baseline_top_decile_mean']} "
              f"-> beats_baseline={call_vs_track5['beats_baseline']} crosses_to_positive={call_vs_track5['crosses_to_positive']}")
        print(f"  PUT:  joint={put_vs_track5['joint_top_decile_mean']} vs track5={put_vs_track5['baseline_top_decile_mean']} "
              f"-> beats_baseline={put_vs_track5['beats_baseline']} crosses_to_positive={put_vs_track5['crosses_to_positive']}")
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
