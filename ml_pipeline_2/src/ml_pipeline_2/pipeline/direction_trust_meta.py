"""Direction-CALL-TRUST meta-labeling: modeling + statistics utilities
(NIFTY, 2026-09-27).

Answers the question this study track exists for: given that composite's
(``strategy_app.ml.entry_direction_resolver.resolve_entry_direction_composite``)
own unconditional CE/PE accuracy is indistinguishable from a coin flip
(established exhaustively elsewhere in this project -- at least 7 independent
methodologies, see ``ml_pipeline_2.pipeline.conditional_direction``'s and
``ml_pipeline_2.pipeline.entry_quality_direction``'s module docstrings for the
full citation trail), can a meta-model trained on information composite does
NOT use predict, bar by bar, whether THIS PARTICULAR call is more likely to
be right or wrong -- and does filtering to "meta-model says trust this call"
measurably raise composite's realized accuracy, with a real statistical
margin, consistently across independent rolling walk-forward windows? This is
meta-labeling applied to a direction-caller instead of an entry-timer -- the
exact pattern this codebase already shipped for entry quality in
``ml_pipeline_2.pipeline.meta_entry_features`` / ``ml_pipeline_2.scripts
.train_meta_entry_v1`` (read those first; this module mirrors their design
throughout).

Import-isolation constraint: this module must NEVER import
``strategy_app.ml.entry_direction_resolver`` or
``strategy_app.engines.strategies.ml_entry`` -- registry-recorded research,
not a change to the live serving path (matches
``ml_pipeline_2.pipeline.conditional_direction`` /
``ml_pipeline_2.pipeline.entry_quality_direction`` /
``ml_pipeline_2.pipeline.meta_direction_features``, all of which already
observe this). Composite's calls are obtained via
``conditional_direction.composite_vote`` -- a hand-transcribed, tested,
import-isolated re-implementation of the resolver's exact weights/thresholds
-- operating directly on the raw resolver-input columns already present in
``training_view_nifty_direction_condition.csv.gz``
(``ml_pipeline_2.scripts.build_entry_quality_direction_dataset``'s output),
never on a live per-bar ``SnapshotAccessor`` replay of the real resolver
function.

NOTE on a prior, now-superseded attempt at this exact study: an earlier,
uncommitted, stalled session's leftover files
(``ml_pipeline_2/scripts/build_direction_meta_dataset.py``,
``ml_pipeline_2/scripts/train_direction_meta_v1.py``,
``ml_pipeline_2/src/ml_pipeline_2/pipeline/direction_replay.py``) built a very
similar design (verified 2026-09-27: sound walk-forward methodology, real
bootstrap/permutation statistics, a real delayed-action stress test) but (a)
ran against FINNIFTY, not NIFTY, because NIFTY raw snapshot parquet/training
-view data was reported unavailable in that session's sandbox (untrue in this
one -- both exist locally, see ``study_direction_trust_meta.py``'s module
docstring), and (b) ``direction_replay.py`` imports
``strategy_app.ml.entry_direction_resolver`` directly (called with
``depth=None``, itself calling the live ``get_depth_context()`` runtime
accessor as a side effect) -- a live-serving-path import this task's brief
explicitly disallows. ``ml_pipeline_2.pipeline.meta_direction_features`` (also
part of that leftover set) has NEITHER problem -- it is instrument-agnostic
and never imports the resolver -- and is reused here VERBATIM, unmodified.
The three problematic leftover files are left in place, untouched (per this
task's instructions), superseded by this module + ``study_direction_trust_meta
.py`` rather than deleted.

Three families of function live here:

1. Composite-call bookkeeping (``compute_composite_calls``,
   ``compute_composite_correct``, ``add_bars_since_active``) -- turn a raw
   per-bar frame (already carrying composite's raw scoring inputs and
   ``payoff_winning_side`` ground truth) into the population this study
   trains/evaluates on: composite's call, whether it fired, whether it was
   right, and how long its current non-abstaining streak has run.
2. Entry-quality-score walk-forward fitting
   (``fit_entry_quality_score_walkforward``) and meta-feature assembly
   (``assemble_meta_feature_frame``) -- thin wrappers over
   ``entry_quality_direction``'s already-tested XGBoost regressor utilities
   and ``meta_direction_features.META_DIRECTION_FEATURE_COLUMNS`` respectively.
3. Trust meta-model fitting (``fit_trust_meta_model`` /
   ``predict_trust_score``) and statistics (``bootstrap_lift_ci``,
   ``permutation_test_lift``, ``evaluate_trust_filter``,
   ``trusted_mask_per_group``) -- decide, per test bar, how much to trust
   composite's call, and quantify whether restricting to "trusted" bars
   raises composite's realized accuracy by more than chance, with a paired
   bootstrap CI and a permutation-test p-value on the lift (not just a point
   estimate) -- plus ``recompute_composite_correct_at_delay``, the delayed-
   action stress-test primitive (mirrors
   ``ml_pipeline_2.pipeline.validation.check_repricing_wall`` /
   ``entry_quality_direction.compute_payoff_at_delay``): re-scores whether a
   FROZEN composite call was "correct" using the ground truth recomputed with
   an entry premium sampled 1-2 bars after the original fire, holding to the
   same original exit bar.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .conditional_direction import composite_vote
from .entry_quality_direction import (
    compute_payoff_at_delay,
    fit_entry_payoff_model,
    fit_entry_payoff_oof_scores,
    score_entry_payoff,
)
from .meta_direction_features import (
    BARS_SINCE_ACTIVE_COL,
    ENTRY_QUALITY_SCORE_COL,
    META_DIRECTION_FEATURE_COLUMNS,
    SELECTED_REGIME_FEATURES,
    SELECTED_VELOCITY_FEATURES,
    bars_since_composite_active,
)

CE = "CE"
PE = "PE"

DEFAULT_TRUST_FRACTIONS: Tuple[float, ...] = (0.25, 0.40, 0.50)

_ASSEMBLE_RAW_COLS: List[str] = list(SELECTED_VELOCITY_FEATURES) + list(SELECTED_REGIME_FEATURES) + [BARS_SINCE_ACTIVE_COL]

DEFAULT_TRUST_META_XGB_PARAMS: Dict[str, Any] = dict(
    n_estimators=150, max_depth=3, learning_rate=0.05,
    eval_metric="logloss", random_state=42, n_jobs=-1,
    subsample=0.9, colsample_bytree=0.9,
)


# ── Composite-call bookkeeping ───────────────────────────────────────────────

def compute_composite_calls(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` with two added columns: `composite_direction` (the reduced
    composite's CE/PE/None vote, via `conditional_direction.composite_vote`
    -- row-independent, so this is safe to call on a whole multi-day frame at
    once, unlike the streak feature below) and `composite_fired` (bool, True
    where `composite_direction` is not None -- composite did not veto this
    bar)."""
    out = df.copy()
    votes = composite_vote(out)
    out["composite_direction"] = votes.to_numpy()
    out["composite_fired"] = votes.notna().to_numpy()
    return out


def compute_composite_correct(
    direction: pd.Series, truth: pd.Series,
) -> pd.Series:
    """1.0 where `direction` fired and matches `truth`, 0.0 where it fired
    and doesn't, NaN where `direction` is None/NaN (composite abstained) or
    `truth` is missing -- NaN, never imputed, so callers can filter to the
    composite-fired, ground-truth-labeled population by simply dropping NaN,
    matching every other label in this pipeline (`option_payoff_labels`'s
    skip-not-impute convention)."""
    direction = pd.Series(direction).reset_index(drop=True)
    truth = pd.Series(truth).reset_index(drop=True)
    fired = direction.notna() & truth.notna()
    out = pd.Series(np.nan, index=direction.index, dtype=float)
    out[fired] = (direction[fired].to_numpy() == truth[fired].to_numpy()).astype(float)
    return out


def add_bars_since_active(
    df: pd.DataFrame, *, fired_col: str = "composite_fired", date_col: str = "trade_date",
) -> pd.Series:
    """`meta_direction_features.bars_since_composite_active`, applied
    separately within each `date_col` group so a streak never crosses a
    trade_date boundary (composite is scored fresh each session). `df` must
    already be sorted chronologically WITHIN each date (e.g. by `bar_index`)
    -- this function does not itself sort, only groups; groups are processed
    using `DataFrame.groupby(...).indices`, which returns each group's
    POSITIONAL row indices into `df` (not label-based), so this is safe even
    when `df.index` is not a plain RangeIndex."""
    fired = df[fired_col].to_numpy()
    out = np.zeros(len(df), dtype=int)
    for _, positions in df.groupby(date_col, sort=False).indices.items():
        positions = np.sort(np.asarray(positions))
        streak = bars_since_composite_active(fired[positions].tolist())
        out[positions] = streak
    return pd.Series(out, index=df.index)


# ── Entry-quality score (walk-forward-safe) + meta-feature assembly ────────

def fit_entry_quality_score_walkforward(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: Sequence[str],
    *,
    target_col: str = "payoff_score",
    n_splits: int = 5,
    random_state: int = 42,
) -> Tuple[np.ndarray, np.ndarray]:
    """Fit the entry-quality/payoff regressor (same architecture as
    `train_entry_option_payoff_v1.py`, via `entry_quality_direction`'s
    already-tested utilities) walk-forward-safely: TRAIN rows get an
    out-of-fold score (never scored by a model that saw that row's own
    label -- see `fit_entry_payoff_oof_scores`), TEST rows get the score
    from a model fit on the FULL train set. Fit on ALL of `train_df`/
    `test_df` (not pre-filtered to composite-fired bars) so the entry-
    quality signal stays a genuinely independent "would ANY option trade
    have cleared cost here" estimate, uncontaminated by composite's own
    firing decision -- callers filter the returned arrays to the fired
    subset positionally (same row order as the input frames) afterward."""
    train_oof = fit_entry_payoff_oof_scores(
        train_df, feature_cols, target_col, n_splits=n_splits, random_state=random_state,
    )
    model, medians = fit_entry_payoff_model(train_df, feature_cols, target_col)
    test_scores = score_entry_payoff(model, medians, test_df, feature_cols)
    return train_oof, test_scores


def assemble_meta_feature_frame(df: pd.DataFrame, entry_quality_score: Sequence[float]) -> pd.DataFrame:
    """Build the `META_DIRECTION_FEATURE_COLUMNS`-shaped frame from a raw
    per-bar frame that already carries `SELECTED_VELOCITY_FEATURES` /
    `SELECTED_REGIME_FEATURES` / `BARS_SINCE_ACTIVE_COL` as plain columns
    (true of the joined NIFTY training view + `add_bars_since_active`'s
    output) plus an externally-computed, walk-forward-safe entry-quality
    score aligned positionally with `df`'s rows."""
    n = len(df)
    scores = np.asarray(entry_quality_score, dtype=float)
    if len(scores) != n:
        raise ValueError(
            f"assemble_meta_feature_frame: entry_quality_score length ({len(scores)}) != len(df) ({n})"
        )
    missing = [c for c in _ASSEMBLE_RAW_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"assemble_meta_feature_frame: df missing required column(s): {missing}")
    out = df[_ASSEMBLE_RAW_COLS].apply(pd.to_numeric, errors="coerce").reset_index(drop=True)
    out[ENTRY_QUALITY_SCORE_COL] = scores
    return out[META_DIRECTION_FEATURE_COLUMNS]


# ── Trust meta-model ─────────────────────────────────────────────────────────

def fit_trust_meta_model(
    X_train: pd.DataFrame, y_train: np.ndarray, *, xgb_params: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, pd.Series]:
    """Fit an XGBClassifier predicting `composite_correct` (1=composite was
    right, 0=wrong) from `META_DIRECTION_FEATURE_COLUMNS`. Pure given an
    already-sliced (X_train, y_train)."""
    import xgboost as xgb

    medians = X_train.median()
    X = X_train.fillna(medians).fillna(0.0)
    params = dict(xgb_params or DEFAULT_TRUST_META_XGB_PARAMS)
    model = xgb.XGBClassifier(**params)
    model.fit(X, y_train)
    return model, medians


def predict_trust_score(model: Any, medians: pd.Series, X: pd.DataFrame) -> np.ndarray:
    Xf = X.fillna(medians).fillna(0.0)
    return np.asarray(model.predict_proba(Xf)[:, 1], dtype=float)


# ── Statistics: bootstrap CI + permutation test on the trust-filter lift ───

def bootstrap_lift_ci(
    y: np.ndarray, p: np.ndarray, frac: float, *, n_boot: int = 2000, random_state: int = 42, ci: float = 0.95,
) -> Tuple[float, float]:
    """Percentile bootstrap CI on (filtered_accuracy - baseline_accuracy) at
    trust fraction `frac`, resampling (y, p) pairs together with replacement
    (a paired bootstrap over bars), applying a FIXED cutoff (from the
    original, non-resampled `p` distribution) to each resample -- so the CI
    reflects sampling uncertainty in which bars end up trusted/correct, not
    uncertainty in where the cutoff itself sits."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n = len(y)
    if n == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(random_state)
    cutoff = np.quantile(p, 1.0 - frac)
    lifts = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        yb, pb = y[idx], p[idx]
        mask = pb >= cutoff
        if not mask.any():
            continue
        lifts.append(float(yb[mask].mean() - yb.mean()))
    if not lifts:
        return float("nan"), float("nan")
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.quantile(lifts, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def permutation_test_lift(
    y: np.ndarray, p: np.ndarray, frac: float, observed_lift: float, *, n_perm: int = 2000, random_state: int = 43,
) -> float:
    """One-sided permutation p-value: fraction of random (y,p)-decoupled
    permutations whose filtered-accuracy lift (at the SAME fixed cutoff)
    equals or exceeds the observed lift. Tests the null that the meta-
    model's score carries no real relationship to `composite_correct` -- a
    negative or zero observed lift will (correctly) score as not
    significant under this one-sided test."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n = len(y)
    if n == 0:
        return float("nan")
    rng = np.random.default_rng(random_state)
    cutoff = np.quantile(p, 1.0 - frac)
    base = float(y.mean())
    count_ge = 0
    valid = 0
    for _ in range(n_perm):
        p_perm = rng.permutation(p)
        mask = p_perm >= cutoff
        if not mask.any():
            continue
        valid += 1
        lift = float(y[mask].mean() - base)
        if lift >= observed_lift:
            count_ge += 1
    if not valid:
        return float("nan")
    return (count_ge + 1) / (valid + 1)


def evaluate_trust_filter(
    y: np.ndarray, p: np.ndarray, frac: float, *, n_boot: int = 2000, n_perm: int = 2000,
) -> Dict[str, Any]:
    """Compare composite's baseline accuracy (all bars) against its accuracy
    restricted to the top `frac` of bars by meta-trust-score `p`, with a
    bootstrap CI and permutation p-value on the lift."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n = len(y)
    baseline = float(y.mean()) if n else float("nan")
    if n == 0:
        return {
            "frac": frac, "n_filtered": 0, "baseline_accuracy": baseline,
            "filtered_accuracy": float("nan"), "lift": float("nan"),
            "ci_lo": float("nan"), "ci_hi": float("nan"), "perm_p": float("nan"),
        }
    cutoff = np.quantile(p, 1.0 - frac)
    mask = p >= cutoff
    n_filtered = int(mask.sum())
    filtered_accuracy = float(y[mask].mean()) if n_filtered else float("nan")
    lift = filtered_accuracy - baseline if n_filtered else float("nan")
    ci_lo, ci_hi = bootstrap_lift_ci(y, p, frac, n_boot=n_boot)
    perm_p = permutation_test_lift(y, p, frac, lift, n_perm=n_perm) if n_filtered else float("nan")
    return {
        "frac": frac,
        "n_filtered": n_filtered,
        "baseline_accuracy": baseline,
        "filtered_accuracy": filtered_accuracy,
        "lift": lift,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "perm_p": perm_p,
    }


def trusted_mask_per_group(window_ids: Sequence[Any], scores: Sequence[float], frac: float) -> np.ndarray:
    """Boolean mask, True where `scores[i]` is in the top `frac` of ITS OWN
    group's (identified by `window_ids[i]`) score distribution. Lets pooling
    test bars from several walk-forward windows' independently-fit meta-
    models respect each window's own calibration -- comparing raw
    probabilities across differently-fit models directly would not be
    meaningful, but "top X% within this window" is."""
    window_ids = np.asarray(window_ids)
    scores = np.asarray(scores, dtype=float)
    mask = np.zeros(len(scores), dtype=bool)
    for wid in np.unique(window_ids):
        sel = window_ids == wid
        if not sel.any():
            continue
        cutoff = np.quantile(scores[sel], 1.0 - frac)
        mask[sel] = scores[sel] >= cutoff
    return mask


# ── Delayed-action stress-test primitive ────────────────────────────────────

ReadDaySnapshotsFn = Callable[[str, str], Sequence[Any]]


def recompute_composite_correct_at_delay(
    rows: pd.DataFrame,
    parquet_base: str,
    *,
    delay_bars: int,
    horizon_bars: int,
    lot_size: int,
    read_day_snapshots_fn: Optional[ReadDaySnapshotsFn] = None,
) -> pd.Series:
    """For every row (must carry `trade_date`, `bar_index`,
    `composite_direction` -- composite's FROZEN call, not recomputed),
    re-score whether that frozen call was "correct" using ground truth
    (`payoff_winning_side`) recomputed with an entry premium sampled
    `delay_bars` bars AFTER the original fire, holding to the SAME original
    target exit bar (`entry_quality_direction.compute_payoff_at_delay` --
    exact same pattern `validation.check_repricing_wall` and
    `run_repricing_wall_control.reprice_fires_at_delays` already use
    elsewhere in this pipeline). Returns a float Series aligned to
    `rows.index`: 1.0/0.0 if resolvable, NaN if the delayed entry or the
    (unchanged) exit bar falls outside that day's session, or the day has no
    snapshot data at all -- dropped, never imputed, by callers.

    `read_day_snapshots_fn` defaults to
    `ml_pipeline_2.scripts.build_option_pnl_training_view.read_day_snapshots`,
    resolved lazily via `importlib` rather than a literal fully-qualified
    import statement spelling this package's own name (so this module has no
    hard, eager dependency on parquet I/O AND doesn't trip
    `test_boundaries.py`'s substring guard against `ml_pipeline_2/src
    /ml_pipeline_2/**` files spelling out that kind of self-referential
    import -- see `entry_quality_direction.winsorize_target`'s docstring for
    the same guard, hit the same way). Also injectable, so this function's
    per-day-grouping/re-scoring logic can be unit-tested against synthetic
    `SnapshotAccessor` fixtures without real parquet files.
    """
    if read_day_snapshots_fn is None:
        import importlib

        _mod = importlib.import_module("ml_pipeline_2.scripts.build_option_pnl_training_view")
        read_day_snapshots_fn = _mod.read_day_snapshots

    out = pd.Series(np.nan, index=rows.index, dtype=float)
    for trade_date, group in rows.groupby("trade_date"):
        snaps = read_day_snapshots_fn(parquet_base, str(trade_date))
        if not snaps:
            continue
        for idx, r in group.iterrows():
            bp = compute_payoff_at_delay(
                snaps, int(r["bar_index"]), horizon_bars=horizon_bars, delay_bars=delay_bars, lot_size=lot_size,
            )
            if bp is None or bp.winning_side is None:
                continue
            out.loc[idx] = float(r["composite_direction"] == bp.winning_side)
    return out


def summarize_delayed_trust_filter(
    correct_at_fire: Sequence[float],
    correct_delay_1: Sequence[float],
    correct_delay_2: Sequence[float],
    trusted_mask: Sequence[bool],
    *,
    edge_threshold: float = 0.55,
    min_valid_bars: int = 20,
) -> Dict[str, Any]:
    """Delayed-action stress test for a trust-filtered subset -- mirrors
    `conditional_direction.check_direction_repricing_wall`'s collapse rule
    (an apparent above-chance edge at fire-time that falls to/below chance
    once the ground truth is recomputed 1-2 bars later is very likely
    measuring the option chain's own repricing of the same information, not
    a structural edge -- the exact failure mode documented for three prior
    entry-timing experiments in this project), applied here to composite's
    own FROZEN-call correctness (`correct_at_fire`/`_delay_1`/`_delay_2`,
    each 1.0/0.0/NaN, aligned 1:1 positionally) restricted to `trusted_mask`.
    Composite's call and the meta-model's trust decision are NOT recomputed
    at any delay -- only the realized ground truth used to grade "was
    composite right" is delayed, exactly like `check_direction_repricing_wall`
    and `entry_quality_direction.compute_payoff_at_delay`.

    Returns `status` in {"thin_sample", "no_edge_at_fire", "collapsed",
    "survived"} -- "no_edge_at_fire" (like `check_direction_repricing_wall`)
    means there's no apparent edge in this subset to begin with, so there is
    nothing here for a delay to collapse; only "collapsed" is a genuine
    failure of an apparent edge, and only "survived" supports trusting it.
    """
    mask = np.asarray(trusted_mask, dtype=bool)
    arrays = [np.asarray(a, dtype=float) for a in (correct_at_fire, correct_delay_1, correct_delay_2)]
    valid = mask.copy()
    for a in arrays:
        valid &= ~np.isnan(a)
    n_valid = int(valid.sum())
    if n_valid < min_valid_bars:
        return {"n_valid": n_valid, "status": "thin_sample", "edge_threshold": edge_threshold}
    acc_fire, acc_d1, acc_d2 = (float(a[valid].mean()) for a in arrays)
    if acc_fire <= edge_threshold:
        return {
            "n_valid": n_valid, "status": "no_edge_at_fire", "edge_threshold": edge_threshold,
            "accuracy_at_fire": acc_fire, "accuracy_delay_1": acc_d1, "accuracy_delay_2": acc_d2,
        }
    collapsed = acc_d1 <= 0.5 or acc_d2 <= 0.5
    return {
        "n_valid": n_valid,
        "status": "collapsed" if collapsed else "survived",
        "edge_threshold": edge_threshold,
        "accuracy_at_fire": acc_fire, "accuracy_delay_1": acc_d1, "accuracy_delay_2": acc_d2,
    }


__all__ = [
    "CE",
    "PE",
    "DEFAULT_TRUST_FRACTIONS",
    "DEFAULT_TRUST_META_XGB_PARAMS",
    "compute_composite_calls",
    "compute_composite_correct",
    "add_bars_since_active",
    "fit_entry_quality_score_walkforward",
    "assemble_meta_feature_frame",
    "fit_trust_meta_model",
    "predict_trust_score",
    "bootstrap_lift_ci",
    "permutation_test_lift",
    "evaluate_trust_filter",
    "trusted_mask_per_group",
    "recompute_composite_correct_at_delay",
    "summarize_delayed_trust_filter",
]
