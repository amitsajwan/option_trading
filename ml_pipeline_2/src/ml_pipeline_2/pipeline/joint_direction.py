"""ONE joint multiclass direction model (NIFTY, 2026-09-27) -- sixth
direction-research track today, and the first to test whether framing
"NO_TRADE / CALL / PUT" as a SINGLE three-way decision (one XGBoost
multiclass classifier, ``objective="multi:softprob"``) finds something the
fifth track's two INDEPENDENT regressors
(``ml_pipeline_2.pipeline.direction_margin`` /
``ml_pipeline_2.scripts.train_direction_margin_v1``) structurally cannot:
an interaction pattern between "is this bar tradeable at all" and "which
side" learned from SHARED tree splits, rather than two isolated models that
each only ever see their own leg's target in isolation.

Track 5's own finding (2026-09-27, ``f4f94194f1``): a call-specific
regressor showed real, walk-forward-consistent DEFENSIVE ranking skill
(pooled Spearman rho=0.076, positive in 5/5 windows) -- it reliably finds
the WORST bars (decile 1 mean -6.4%) but its own top decile was still
NEGATIVE (-0.55%), failing the ship gate's ``top_decile_mean_payoff>0``
criterion. The put regressor showed no real independent skill at all
(rho=0.014, inconsistent sign). This module's central, honest question:
does training ONE joint decision instead of two isolated ones turn either
leg's defensive-only (or null) signal into a genuinely profitable
high-confidence subset -- not just a re-shuffling of the same information.

This is registry-recorded research only. It must NEVER import
``strategy_app.ml.entry_direction_resolver``,
``strategy_app.engines.strategies.ml_entry``, ``strategy_app.brain
.regime_director``, or ``strategy_app.market.regime``.

Label definition (from ``training_view_nifty_direction_margin.csv.gz``'s
existing ``net_ce_return``/``net_pe_return`` columns -- the SAME per-bar,
post-cost leg returns track 5 used, no new label build needed):

    NO_TRADE  if max(net_ce_return, net_pe_return) <= 0   (neither leg would
                                                             have cleared cost)
    CALL      elif net_ce_return >  net_pe_return
    PUT       else                                         (net_pe_return >=
                                                             net_ce_return,
                                                             including exact
                                                             ties -- a
                                                             deterministic,
                                                             documented
                                                             tie-break, not
                                                             an ambiguous
                                                             third case)

Rows where EITHER leg's net return is missing get label=None (dropped by
callers, never imputed -- matches every other label in this pipeline). A
real 2026-09-27 measurement against the full NIFTY history (137,285 rows,
2024-11-04..2026-07-31) found 136,261 rows (99.25%) have both legs usable,
and the resulting class balance is CALL=41.9%, PUT=41.3%, NO_TRADE=16.8% --
confirms the task's expectation (track 1's ~83% "some leg clears cost"
finding implies NO_TRADE should be the rarer, more informative class here,
not the majority).

Three families of function live here, all pure and independently testable
with synthetic fixtures (no model, no snapshot parquet needed):

1. Label derivation (``compute_three_class_label`` / ``encode_label`` /
   ``decode_label`` / ``class_balance``) -- the 3-class target itself, and
   the int<->str codec ``xgboost``'s ``multi:softprob`` needs (XGBoost
   requires 0-indexed integer class labels; ``CLASS_TO_INT`` fixes the
   column order of the probability matrix XGBoost returns:
   column 0=NO_TRADE, 1=CALL, 2=PUT).
2. Prediction-side primitives (``argmax_class`` / ``class_actual_value`` /
   ``top_decile_mask_within_argmax``) -- turning a raw (n, 3) probability
   matrix into a decision (argmax) and picking out the "confidently called
   this class" subset the decision-relevant evaluation needs.
3. The decision-relevant evaluation itself (``class_decile_eval`` /
   ``compute_no_trade_ship_gates``) -- the decile-lift-style test item 3 of
   the owning task asks for, deliberately built to REUSE
   ``train_entry_option_payoff_v1.decile_lift_table`` verbatim (imported
   lazily, inside the function, to avoid a module-level import cycle with
   the training script) rather than reimplementing decile bucketing a
   fourth time in this project.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

NO_TRADE = "NO_TRADE"
CALL = "CALL"
PUT = "PUT"

# Column order of the (n, 3) probability matrix XGBoost's multi:softprob
# returns is fixed by the INTEGER label a row was trained with -- this dict
# is the single source of truth for that mapping, used both when encoding
# training labels and when decoding predict_proba's columns back to names.
CLASS_TO_INT: Dict[str, int] = {NO_TRADE: 0, CALL: 1, PUT: 2}
INT_TO_CLASS: Dict[int, str] = {v: k for k, v in CLASS_TO_INT.items()}
CLASS_NAMES: Tuple[str, str, str] = (NO_TRADE, CALL, PUT)

# Ship-gate defaults for the NO_TRADE class -- deliberately NOT
# `train_entry_option_payoff_v1.compute_ship_gates` (that function expects a
# POSITIVE top-decile mean, i.e. "high confidence -> high payoff"; NO_TRADE's
# expected-good direction is the OPPOSITE -- "high confidence NO_TRADE ->
# LOW best-achievable return", i.e. a NEGATIVE spearman rho and a top decile
# (highest P(NO_TRADE)) mean that is LOWER than the bottom decile's).
NO_TRADE_SHIP_GATE_SPEARMAN_MAX = -0.05
NO_TRADE_SHIP_GATE_SPEARMAN_P_MAX = 0.05
NO_TRADE_SHIP_GATE_MIN_ROWS = 200


def compute_three_class_label(
    net_ce_return: Sequence[Optional[float]], net_pe_return: Sequence[Optional[float]],
) -> pd.Series:
    """NO_TRADE / CALL / PUT per bar -- see module docstring for the exact
    rule and its tie-break convention. None (never imputed) wherever EITHER
    leg's net return is missing."""
    ce = pd.to_numeric(pd.Series(net_ce_return), errors="coerce")
    pe = pd.to_numeric(pd.Series(net_pe_return), errors="coerce")
    both = (ce.notna() & pe.notna()).to_numpy()
    ce_v = ce.to_numpy(dtype=float)
    pe_v = pe.to_numpy(dtype=float)
    best = np.maximum(ce_v, pe_v)
    label = np.where(best <= 0, NO_TRADE, np.where(ce_v > pe_v, CALL, PUT))
    out = np.where(both, label, None)
    return pd.Series(out, index=ce.index, dtype=object)


def encode_label(labels: Sequence[Optional[str]]) -> np.ndarray:
    """String labels -> XGBoost-ready 0/1/2 ints. Raises on anything not in
    CLASS_TO_INT (including None) -- callers must drop unresolved rows
    BEFORE encoding, matching `entry_quality_direction.encode_side_binary`'s
    same fail-loud convention (a silent NaN-coerce here would hide a real
    upstream bug rather than surface it)."""
    out = np.empty(len(labels), dtype=int)
    for i, v in enumerate(labels):
        if v not in CLASS_TO_INT:
            raise ValueError(f"encode_label: value {v!r} at position {i} is not one of {CLASS_NAMES}")
        out[i] = CLASS_TO_INT[v]
    return out


def decode_label(ints: Sequence[int]) -> np.ndarray:
    """0/1/2 ints -> string labels, inverse of `encode_label`."""
    out = np.empty(len(ints), dtype=object)
    for i, v in enumerate(ints):
        iv = int(v)
        if iv not in INT_TO_CLASS:
            raise ValueError(f"decode_label: value {v!r} at position {i} is not one of {sorted(INT_TO_CLASS)}")
        out[i] = INT_TO_CLASS[iv]
    return out


def class_balance(labels: Sequence[Optional[str]]) -> Dict[str, Any]:
    """Class-count/fraction report -- item 1 of the owning task
    ("report the class balance ... flag clearly if it doesn't match
    expectation"). Rows with label=None (either leg missing) are counted
    separately and excluded from the fractions, which are of resolved rows
    only."""
    s = pd.Series(labels)
    resolved = s[s.notna()]
    n_total = int(len(s))
    n_resolved = int(len(resolved))
    counts = {c: int((resolved == c).sum()) for c in CLASS_NAMES}
    fractions = {c: (round(counts[c] / n_resolved, 4) if n_resolved else None) for c in CLASS_NAMES}
    return {
        "n_total": n_total,
        "n_resolved": n_resolved,
        "n_unresolved": n_total - n_resolved,
        "counts": counts,
        "fractions": fractions,
    }


def argmax_class(proba: np.ndarray) -> np.ndarray:
    """(n, 3) probability matrix (columns ordered per CLASS_TO_INT) -> (n,)
    array of predicted class NAMES (never raw ints), so every downstream
    consumer compares against the same CALL/PUT/NO_TRADE vocabulary the
    label itself uses."""
    proba = np.asarray(proba, dtype=float)
    if proba.ndim != 2 or proba.shape[1] != len(CLASS_NAMES):
        raise ValueError(f"argmax_class: expected an (n, {len(CLASS_NAMES)}) matrix, got shape {proba.shape}")
    idx = np.argmax(proba, axis=1)
    return decode_label(idx)


def class_actual_value(
    net_ce_return: Sequence[Optional[float]],
    net_pe_return: Sequence[Optional[float]],
    *,
    target_class: str,
) -> np.ndarray:
    """The ACTUAL, decision-relevant outcome for `target_class` at each bar
    -- CALL -> net_ce_return, PUT -> net_pe_return, NO_TRADE ->
    max(net_ce_return, net_pe_return) (the best achievable return with
    perfect hindsight, i.e. "what did NO_TRADE actually forgo/correctly
    avoid"). NaN (never imputed) wherever the relevant leg(s) are missing."""
    ce = pd.to_numeric(pd.Series(net_ce_return), errors="coerce").to_numpy(dtype=float)
    pe = pd.to_numeric(pd.Series(net_pe_return), errors="coerce").to_numpy(dtype=float)
    if len(ce) != len(pe):
        raise ValueError("class_actual_value: net_ce_return and net_pe_return must be the same length")
    if target_class == CALL:
        return ce
    if target_class == PUT:
        return pe
    if target_class == NO_TRADE:
        return np.maximum(ce, pe)
    raise ValueError(f"class_actual_value: unknown target_class={target_class!r}, expected one of {CLASS_NAMES}")


def top_decile_mask_within_argmax(
    proba_target: Sequence[float],
    argmax_labels: Sequence[str],
    *,
    target_class: str,
    frac: float = 0.10,
) -> np.ndarray:
    """Boolean mask (aligned to the full input length) selecting the top
    `frac` of `proba_target` (P(target_class)) among ONLY the bars where
    `target_class` is already the model's argmax -- "when the model
    predicts CALL with high confidence" per the owning task's item 3(b),
    NOT top-`frac` of P(CALL) unconditionally (which could include bars the
    model would never actually act on, e.g. a PUT-argmax bar with a
    middling-but-not-highest P(CALL)). Single-tailed, unlike
    `direction_margin.select_confident_subset_mask`'s two-tailed design --
    there is no symmetric "other side" here, each class is evaluated fully
    independently, exactly mirroring track 5's per-leg (not per-margin)
    standalone evaluation."""
    proba = np.asarray(proba_target, dtype=float)
    argmax = np.asarray(argmax_labels, dtype=object)
    if len(proba) != len(argmax):
        raise ValueError("top_decile_mask_within_argmax: proba_target and argmax_labels must be the same length")
    out = np.zeros(len(proba), dtype=bool)
    idx = np.where(argmax == target_class)[0]
    if len(idx) == 0:
        return out
    k = max(1, int(round(len(idx) * frac)))
    order = np.argsort(proba[idx], kind="mergesort")
    top_idx = idx[order[-k:]]
    out[top_idx] = True
    return out


def class_decile_eval(
    proba_target: Sequence[float],
    actual_value: Sequence[Optional[float]],
    argmax_labels: Sequence[str],
    *,
    target_class: str,
    n_deciles: int = 10,
) -> Dict[str, Any]:
    """The decision-relevant, decile-lift-style evaluation for ONE class,
    restricted to bars where `target_class` is the model's own argmax (the
    bars it would actually act on) -- reuses
    `train_entry_option_payoff_v1.decile_lift_table` VERBATIM (imported
    lazily to dodge a module import cycle) rather than reimplementing
    decile bucketing again. `actual_value` should already be the RIGHT
    outcome series for `target_class` (see `class_actual_value`) -- this
    function itself is target-agnostic about what "actual" means, so it
    doubles as the NO_TRADE evaluation too (actual_value=max(ce, pe) there)
    without any special-casing.

    For CALL/PUT, a real signal looks like: positive spearman_rho (higher
    P(class) -> higher actual own-leg return) and a positive top-decile
    mean. For NO_TRADE, a real signal looks like the OPPOSITE sign:
    NEGATIVE spearman_rho (higher P(NO_TRADE) -> LOWER best-achievable
    return) -- callers should use `compute_no_trade_ship_gates`, not
    `train_entry_option_payoff_v1.compute_ship_gates`, to judge that case."""
    from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import decile_lift_table
    from scipy.stats import spearmanr

    proba = np.asarray(proba_target, dtype=float)
    actual = pd.to_numeric(pd.Series(actual_value), errors="coerce").to_numpy(dtype=float)
    argmax = np.asarray(argmax_labels, dtype=object)
    if not (len(proba) == len(actual) == len(argmax)):
        raise ValueError("class_decile_eval: proba_target, actual_value, argmax_labels must be the same length")

    argmax_mask = argmax == target_class
    n_argmax = int(argmax_mask.sum())
    sub_proba = proba[argmax_mask]
    sub_actual = actual[argmax_mask]
    valid = ~np.isnan(sub_actual)
    n = int(valid.sum())
    base = {
        "target_class": target_class, "n_argmax": n_argmax, "rows": n,
        "spearman_rho": None, "spearman_p_value": None, "decile_table": [],
        "top_decile_mean": None, "bottom_decile_mean": None, "argmax_mean_actual": None,
    }
    if n < 2:
        return base

    rho, p_value = spearmanr(sub_proba[valid], sub_actual[valid])
    rho = 0.0 if np.isnan(rho) else float(rho)
    p_value = 1.0 if np.isnan(p_value) else float(p_value)
    deciles = decile_lift_table(sub_proba[valid], sub_actual[valid], n_deciles=n_deciles)
    base.update({
        "spearman_rho": round(rho, 4),
        "spearman_p_value": round(p_value, 4),
        "decile_table": deciles,
        "top_decile_mean": deciles[-1]["mean_actual_payoff"] if deciles else None,
        "bottom_decile_mean": deciles[0]["mean_actual_payoff"] if deciles else None,
        "argmax_mean_actual": round(float(sub_actual[valid].mean()), 5),
    })
    return base


def multiclass_diagnostics(true_labels: Sequence[str], pred_labels: Sequence[str]) -> Dict[str, Any]:
    """Standard multiclass diagnostics (item 3(a) of the owning task):
    confusion matrix, per-class precision/recall/F1/support, overall
    accuracy, macro F1, and support-weighted F1. Fixed to `CLASS_NAMES`
    order regardless of which classes happen to appear in a particular
    window's test slice -- a window with zero TRUE NO_TRADE bars, e.g.,
    should still report a well-formed 3x3 confusion matrix (an all-zero
    row), not silently collapse to 2x2."""
    from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

    true_arr = np.asarray(true_labels, dtype=object)
    pred_arr = np.asarray(pred_labels, dtype=object)
    if len(true_arr) != len(pred_arr):
        raise ValueError("multiclass_diagnostics: true_labels and pred_labels must be the same length")
    labels = list(CLASS_NAMES)
    cm = confusion_matrix(true_arr, pred_arr, labels=labels)
    precision, recall, f1, support = precision_recall_fscore_support(
        true_arr, pred_arr, labels=labels, zero_division=0,
    )
    macro_f1 = float(np.mean(f1)) if len(f1) else 0.0
    weighted_f1 = float(np.average(f1, weights=support)) if support.sum() > 0 else 0.0
    per_class = {
        cls: {
            "precision": round(float(precision[i]), 4),
            "recall": round(float(recall[i]), 4),
            "f1": round(float(f1[i]), 4),
            "support": int(support[i]),
        }
        for i, cls in enumerate(labels)
    }
    accuracy = float((true_arr == pred_arr).mean()) if len(true_arr) else None
    return {
        "n": int(len(true_arr)),
        "labels": labels,
        "confusion_matrix": cm.tolist(),
        "per_class": per_class,
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "macro_f1": round(macro_f1, 4),
        "weighted_f1": round(weighted_f1, 4),
    }


def compute_no_trade_ship_gates(
    eval_dict: Dict[str, Any],
    *,
    spearman_max: float = NO_TRADE_SHIP_GATE_SPEARMAN_MAX,
    spearman_p_max: float = NO_TRADE_SHIP_GATE_SPEARMAN_P_MAX,
    min_rows: int = NO_TRADE_SHIP_GATE_MIN_ROWS,
) -> Dict[str, bool]:
    """NO_TRADE-specific ship gates -- the mirror image of
    `train_entry_option_payoff_v1.compute_ship_gates`, with the sign
    flipped because NO_TRADE's "working" direction is the opposite of
    CALL/PUT's: high confidence should predict a LOW (not high)
    best-achievable return. Passes only if the model's NO_TRADE calls are
    doing real, walk-forward-legible work (avoiding bars that really were
    bad) rather than just wrapping noise in a confident-looking label."""
    rows = eval_dict.get("rows", 0)
    rho = eval_dict.get("spearman_rho")
    p_value = eval_dict.get("spearman_p_value")
    top = eval_dict.get("top_decile_mean")
    bottom = eval_dict.get("bottom_decile_mean")
    return {
        f"rows>={min_rows}": rows >= min_rows,
        f"spearman_rho<={spearman_max}": rho is not None and rho <= spearman_max,
        f"spearman_p_value<={spearman_p_max}": p_value is not None and p_value <= spearman_p_max,
        "top_decile_mean<bottom_decile_mean": (
            top is not None and bottom is not None and top < bottom
        ),
    }


def compare_top_decile_to_baseline(
    joint_top_decile_mean: Optional[float], baseline_top_decile_mean: float,
) -> Dict[str, Any]:
    """Pure comparison helper for the owning task's item 4 (THE critical
    comparison): does the joint model's high-confidence CALL top decile do
    genuinely better than track 5's independent call regressor's own top
    decile (-0.55%, real, documented, still net-negative)? `beats_baseline`
    is a strict ordering check; `crosses_to_positive` is the sharper,
    headline-relevant question ("did this find a genuinely profitable
    subset track 5 missed", not just "is this number slightly less bad")."""
    if joint_top_decile_mean is None:
        return {
            "joint_top_decile_mean": None, "baseline_top_decile_mean": baseline_top_decile_mean,
            "beats_baseline": None, "crosses_to_positive": None,
        }
    return {
        "joint_top_decile_mean": round(float(joint_top_decile_mean), 5),
        "baseline_top_decile_mean": round(float(baseline_top_decile_mean), 5),
        "beats_baseline": bool(joint_top_decile_mean > baseline_top_decile_mean),
        "crosses_to_positive": bool(joint_top_decile_mean > 0.0 and baseline_top_decile_mean <= 0.0),
    }


__all__ = [
    "NO_TRADE", "CALL", "PUT", "CLASS_NAMES", "CLASS_TO_INT", "INT_TO_CLASS",
    "compute_three_class_label", "encode_label", "decode_label", "class_balance",
    "argmax_class", "class_actual_value", "top_decile_mask_within_argmax",
    "class_decile_eval", "multiclass_diagnostics", "compute_no_trade_ship_gates", "compare_top_decile_to_baseline",
]
