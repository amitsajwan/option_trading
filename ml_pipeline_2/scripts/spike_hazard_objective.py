"""Objective-function spike for the hazard/duration-to-move entry-timing
label (NIFTY entry-focus research, 2026-09-26). Compares three ways to fit
a model against a duration-to-event target on a small SYNTHETIC dataset
with a known, designed-in hazard structure (a "compressed"/high-hazard
regime with a short expected time-to-event vs a "calm"/low-hazard regime
with a long one) -- so we know exactly what a correctly-fit model should
discover, unlike on real market data where the ground truth is unknown:

  1. XGBoost's native ``survival:cox`` objective (Cox proportional hazards;
     label = signed time, negative = right-censored).
  2. XGBoost's native ``survival:aft`` objective (accelerated failure time;
     label = a [lower, upper] bound pair set via `DMatrix.set_float_info`).
  3. A plain discrete time-bucket reformulation: bin `time_to_event` into
     ordinal buckets and train an ordinary multiclass `XGBClassifier` --
     exactly the same model class `train_entry_dhan_v3.run_hpo` already
     uses for the existing binary label, just with more classes.

Run directly: ``python -m ml_pipeline_2.scripts.spike_hazard_objective``

xgboost==3.2.0 is pinned in this repo's venv (`ml_pipeline_2/pyproject.toml`,
kept in lock-step with the serving images) -- confirmed installed before
writing this; `lifelines`/`scikit-survival` are NOT installed and are not
added here (nothing in this spike needs them: `xgboost.DMatrix` covers both
survival objectives natively).

Label-encoding conventions used below were confirmed from XGBoost's own
source/docs before writing this code, not assumed:
  - ``survival:cox``: "negative values are considered right censored"
    (XGBoost's own `parameter.rst`); the observed-vs-censored indicator is
    literally `label > 0` in `CoxRegression::GetGradient`
    (`src/objective/regression_obj.cu`) -- so a label of exactly `+0.0` or
    `-0.0` is ALWAYS treated as censored (0 is not `> 0`), sign notwith-
    standing. `CoxRegression::PredTransform` applies `exp(raw_margin)`, so
    `Booster.predict()` returns a RISK SCORE (higher = higher hazard =
    sooner event), not a time.
  - ``survival:aft``: label is a `[lower, upper]` bound pair via
    `DMatrix.set_float_info("label_lower_bound", ...)` /
    `set_float_info("label_upper_bound", ...)` -- uncensored is `[t, t]`,
    right-censored is `[t, +inf)` (XGBoost's AFT tutorial). `Booster.
    predict()` for AFT is directly comparable to those same bounds
    (confirmed against XGBoost's own `aft_survival_demo.py`, which prints
    predictions next to the label bounds unmodified) -- i.e. already a time,
    not a log-time or a risk score.

See the top of ``ml_pipeline_2.pipeline.hazard_labels`` for this spike's
resulting recommendation (discrete bucket) and the reasoning; this module
is the runnable evidence behind that recommendation, not a restatement of
it.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.hazard_labels import concordance_index  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spike_hazard_objective")

MAX_TIME = 20  # bars; the synthetic "session length" a censored observation ran out at
# Upper edges of 5 ordinal buckets: [1,3) / [3,6) / [6,10) / [10,15) / [15,+inf-or-censored).
# time_to_event is always >= 1 by construction (both the real label -- an
# event needs at least the next bar, k>=1 -- and this spike's synthetic
# geometric draw), so bucket 0's implicit lower edge is 1, not 0.
BUCKET_EDGES = [3, 6, 10, 15]
N_BUCKETS = len(BUCKET_EDGES) + 1  # +1 for the "15+ or censored" catch-all bucket


# ── Synthetic data with a known, designed-in hazard structure ───────────────

def make_synthetic_hazard_data(
    n: int = 2000, seed: int = 42, max_time: int = MAX_TIME
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """`n` synthetic bars, each in one of two regimes with a KNOWN, designed
    -in hazard rate:
      - regime=1 ("compressed"/high-hazard): short expected time-to-event.
      - regime=0 ("calm"/low-hazard): long expected time-to-event.

    `feat_compression` is an engineered feature correlated with (but not a
    direct copy of) the true regime -- mimicking how a real feature would
    only partially reveal the true underlying state. `feat_unrelated` is
    pure noise, included so a correctly-fit model should assign it near-zero
    importance.

    Each row also gets its own random `censor_time` (1..max_time), simulating
    a REAL bar's varying "bars left until session end" (a bar near session
    end has little runway left before it's censored; a bar near session
    start has almost the whole session) -- NOT one fixed cutoff for every
    row. This matters for the discrete-bucket approach's own labeling
    gotcha below (`_bucket_labels`): a fixed common censor time would make
    every censored row land past the last bucket edge (never ambiguous),
    which is unrealistic and would hide the very gotcha this spike is
    supposed to surface.

    Returns (X, observed_time, event_observed, true_regime).
    """
    rng = np.random.default_rng(seed)
    regime = rng.integers(0, 2, size=n)
    hazard_rate = np.where(regime == 1, 0.35, 0.07)  # per-bar prob of "the move" happening
    true_time = rng.geometric(hazard_rate)  # >= 1
    censor_time = rng.integers(1, max_time + 1, size=n)  # varies per row, like real session-end distance
    event_observed = true_time <= censor_time
    observed_time = np.minimum(true_time, censor_time).astype(float)

    noise = rng.normal(0, 1, size=n)
    feat_compression = regime * 1.5 + noise * 0.6
    feat_unrelated = rng.normal(0, 1, size=n)
    X = pd.DataFrame({"feat_compression": feat_compression, "feat_unrelated": feat_unrelated})
    return X, observed_time, event_observed, regime


def _bucket_index(time_value: float) -> int:
    for i, edge in enumerate(BUCKET_EDGES):
        if time_value < edge:
            return i
    return len(BUCKET_EDGES)  # last bucket: "15+ (or unambiguously-censored past 15)"


def _bucket_labels(observed_time: np.ndarray, event_observed: np.ndarray) -> np.ndarray:
    """Assign an ordinal bucket per row, or -1 ("drop, ambiguous") for a
    censored row whose censor time is BELOW the last finite bucket edge --
    those rows' true bucket is unknowable (the real event could still land
    in any bucket at or after the one their censor time falls into), unlike
    an event that's actually observed. A censored row whose censor time
    already reaches the last edge is unambiguously "15+ or censored" and IS
    kept -- this is the discrete-bucket approach's own labeling gotcha,
    smaller in scope than Cox's sign convention but real: it silently drops
    information (and shrinks the training set) if not handled explicitly.
    """
    last_edge = BUCKET_EDGES[-1]
    out = np.full(len(observed_time), -1, dtype=int)
    for i, (t, observed) in enumerate(zip(observed_time, event_observed)):
        if observed:
            out[i] = _bucket_index(t)
        elif t >= last_edge:
            out[i] = len(BUCKET_EDGES)  # unambiguously the catch-all bucket
        # else: ambiguous censored row below the last edge -- dropped (-1)
    return out


# ── The three model fits ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpikeFitResult:
    name: str
    predicted_time_proxy: np.ndarray  # lower = predicted sooner, for concordance_index
    n_train_rows_used: int
    integration_notes: str


def fit_cox(X_train: pd.DataFrame, time_train: np.ndarray, event_train: np.ndarray,
            X_test: pd.DataFrame) -> SpikeFitResult:
    import xgboost as xgb

    # Sign convention: negative = right-censored. `time_train` from
    # `make_synthetic_hazard_data` is always >= 1 (numpy's geometric starts
    # at 1), so there is no true time-zero row to worry about here -- but a
    # real deployment of this against `hazard_labels.add_hazard_labels`
    # output MUST special-case time_to_event==0 rows (legitimately
    # produced for a bar at the very last row of the session, see that
    # module's docstring) since `label > 0` is xgboost's OWN observed-event
    # test and +0.0/-0.0 both fail it, regardless of sign or intent.
    signed_label = np.where(event_train, time_train, -time_train)
    dtrain = xgb.DMatrix(X_train, label=signed_label)
    dtest = xgb.DMatrix(X_test)
    params = {"objective": "survival:cox", "eval_metric": "cox-nloglik",
              "max_depth": 3, "eta": 0.1, "seed": 42}
    bst = xgb.train(params, dtrain, num_boost_round=100)
    risk = bst.predict(dtest)  # exp(raw_margin) -- HIGHER = sooner event
    predicted_time_proxy = -risk  # invert so LOWER = sooner, matching concordance_index's convention
    return SpikeFitResult(
        name="survival:cox",
        predicted_time_proxy=predicted_time_proxy,
        n_train_rows_used=len(X_train),
        integration_notes=(
            "Single signed-label array, no ranged-label plumbing needed. "
            "predict() returns a risk score (not a time) -- required "
            "negating before it could be compared via concordance_index's "
            "time convention. The label>0-is-observed check silently "
            "swallows any exact-zero time as censored, sign or intent "
            "notwithstanding -- a real landmine for a duration label "
            "(like this project's own add_hazard_labels) that CAN "
            "legitimately produce a censored time of exactly 0."
        ),
    )


def fit_aft(X_train: pd.DataFrame, time_train: np.ndarray, event_train: np.ndarray,
            X_test: pd.DataFrame) -> SpikeFitResult:
    import xgboost as xgb

    lower = time_train.astype(float)
    upper = np.where(event_train, time_train, np.inf).astype(float)
    dtrain = xgb.DMatrix(X_train)
    dtrain.set_float_info("label_lower_bound", lower)
    dtrain.set_float_info("label_upper_bound", upper)
    dtest = xgb.DMatrix(X_test)
    params = {
        "objective": "survival:aft",
        "eval_metric": "aft-nloglik",
        "aft_loss_distribution": "normal",
        "aft_loss_distribution_scale": 1.0,
        "max_depth": 3, "eta": 0.1, "seed": 42,
    }
    bst = xgb.train(params, dtrain, num_boost_round=100)
    predicted_time_proxy = bst.predict(dtest)  # already a time -- lower = sooner, no inversion needed
    return SpikeFitResult(
        name="survival:aft",
        predicted_time_proxy=predicted_time_proxy,
        n_train_rows_used=len(X_train),
        integration_notes=(
            "Needs two float-info arrays on the DMatrix (label_lower_bound/"
            "label_upper_bound) instead of a plain y array -- more label-"
            "encoding surface area than Cox, though each half is individually "
            "simple (uncensored=[t,t], right-censored=[t, +inf)). predict() "
            "output is directly a time, no inversion needed. Requires "
            "choosing an extra hyperparameter family (aft_loss_distribution "
            "+ aft_loss_distribution_scale) that neither Cox nor the "
            "discrete-bucket approach needs -- one more way to mis-specify "
            "the model, and no principled default for this project's data."
        ),
    )


def fit_discrete_bucket(X_train: pd.DataFrame, time_train: np.ndarray, event_train: np.ndarray,
                         X_test: pd.DataFrame) -> SpikeFitResult:
    import xgboost as xgb

    bucket_train = _bucket_labels(time_train, event_train)
    keep = bucket_train >= 0
    n_dropped = int((~keep).sum())
    if n_dropped:
        log.info("discrete-bucket: dropping %d/%d train rows with an ambiguous censored bucket",
                  n_dropped, len(bucket_train))
    X_train_kept, y_train_kept = X_train.loc[keep], bucket_train[keep]

    clf = xgb.XGBClassifier(
        n_estimators=150, max_depth=3, learning_rate=0.1,
        objective="multi:softprob", num_class=N_BUCKETS,
        eval_metric="mlogloss", random_state=42, n_jobs=-1,
    )
    clf.fit(X_train_kept, y_train_kept)
    proba = clf.predict_proba(X_test)  # (n_test, N_BUCKETS)
    bucket_centers = np.array(
        [ (1.0 + BUCKET_EDGES[0]) / 2.0 ]  # bucket 0's implicit lower edge is 1 (see BUCKET_EDGES comment)
        + [ (BUCKET_EDGES[i] + BUCKET_EDGES[i + 1]) / 2.0 for i in range(len(BUCKET_EDGES) - 1) ]
        + [ float(BUCKET_EDGES[-1]) ]  # open-ended last bucket -- use its lower edge as a floor proxy
    )
    predicted_time_proxy = proba @ bucket_centers  # expected bucket-center time -- lower = sooner
    return SpikeFitResult(
        name="discrete_bucket",
        predicted_time_proxy=predicted_time_proxy,
        n_train_rows_used=int(keep.sum()),
        integration_notes=(
            f"Plain X/y arrays into an ordinary XGBClassifier -- the exact "
            f"same shape of call train_entry_dhan_v3.run_hpo already makes "
            f"for the existing binary label, no DMatrix-level label-encoding "
            f"API needed. Its own gotcha: a censored row whose censor time "
            f"is below the last bucket edge has an AMBIGUOUS true bucket and "
            f"must be dropped ({n_dropped} of {len(bucket_train)} rows here) "
            f"-- smaller blast radius than Cox's silent zero-time bug, but "
            f"real, and shrinks the effective training set. Output is a "
            f"probability per duration bucket -- directly interpretable "
            f"without any inversion or a time reconstruction step."
        ),
    )


# ── Ground-truth recovery check ──────────────────────────────────────────────

def regime_separation(predicted_time_proxy: np.ndarray, true_regime: np.ndarray) -> tuple[float, float, float]:
    """Mean predicted-time-proxy for the high-hazard regime vs the
    low-hazard regime, and the gap between them (should be strongly
    negative -- the model should predict a SHORTER time for the regime we
    designed to have a shorter true expected time-to-event). This is the
    "does it recover the known ground truth structure" check -- a plain
    concordance/C-index number alone doesn't tell you WHY a model scored
    well; this does.
    """
    mean_high_hazard = float(predicted_time_proxy[true_regime == 1].mean())
    mean_low_hazard = float(predicted_time_proxy[true_regime == 0].mean())
    return mean_high_hazard, mean_low_hazard, mean_high_hazard - mean_low_hazard


def run_one_seed(seed: int, n: int = 2000, print_notes: bool = False) -> dict[str, dict[str, float]]:
    """Fit all three objectives on one synthetic draw, return
    {objective_name: {"c_index":..., "gap":...}}. A single seed's numbers
    can be noise -- `main()` runs several and reports the average, per
    this repo's own "don't trust a single run" discipline
    (feedback_scrutinize_results_before_reporting)."""
    X, observed_time, event_observed, regime = make_synthetic_hazard_data(n=n, seed=seed)
    n_train = int(len(X) * 0.7)
    X_train, X_test = X.iloc[:n_train], X.iloc[n_train:]
    time_train, time_test = observed_time[:n_train], observed_time[n_train:]
    event_train, event_test = event_observed[:n_train], event_observed[n_train:]
    regime_test = regime[n_train:]

    fits = [
        fit_cox(X_train, time_train, event_train, X_test),
        fit_aft(X_train, time_train, event_train, X_test),
        fit_discrete_bucket(X_train, time_train, event_train, X_test),
    ]
    results: dict[str, dict[str, float]] = {}
    for fit in fits:
        conc = concordance_index(fit.predicted_time_proxy, time_test, event_test)
        _, _, gap = regime_separation(fit.predicted_time_proxy, regime_test)
        results[fit.name] = {"c_index": conc.c_index, "gap": gap,
                              "n_train_rows_used": float(fit.n_train_rows_used)}
        if print_notes:
            print(f"\n[{fit.name}] (n_train_rows_used={fit.n_train_rows_used})\n  {fit.integration_notes}")
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse
    import xgboost

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=2000, help="synthetic bars per seed")
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 123, 2026, 99],
                     help="one run per seed -- multiple seeds guard against a single lucky/unlucky draw")
    args = ap.parse_args(argv)

    log.info("xgboost version: %s", xgboost.__version__)

    per_seed: dict[int, dict[str, dict[str, float]]] = {}
    for i, seed in enumerate(args.seeds):
        per_seed[seed] = run_one_seed(seed, n=args.n, print_notes=(i == 0))

    names = ["survival:cox", "survival:aft", "discrete_bucket"]
    print("\n" + "=" * 90)
    print(f"{'objective':<16}{'mean_c_index':>14}{'mean_gap':>12}{'wins(c_index)':>16}{'wins(gap mag)':>16}")
    print("=" * 90)
    wins_c = {name: 0 for name in names}
    wins_gap = {name: 0 for name in names}
    for seed, res in per_seed.items():
        best_c = max(names, key=lambda nm: res[nm]["c_index"])
        best_gap = max(names, key=lambda nm: abs(res[nm]["gap"]))
        wins_c[best_c] += 1
        wins_gap[best_gap] += 1
    for name in names:
        mean_c = float(np.mean([per_seed[s][name]["c_index"] for s in args.seeds]))
        mean_gap = float(np.mean([per_seed[s][name]["gap"] for s in args.seeds]))
        print(f"{name:<16}{mean_c:>14.4f}{mean_gap:>12.4f}{wins_c[name]:>16d}{wins_gap[name]:>16d}")
    print("=" * 90)
    print(f"(seeds tried: {args.seeds}; 'wins' = how many seeds that objective had the best value)")
    print("\nExpectation check: gap should be clearly NEGATIVE for all three "
          "(high-hazard regime predicted sooner than low-hazard regime) and "
          "mean_c_index should be well above 0.5 -- both confirm the model "
          "found the injected regime structure rather than fitting noise.\n")
    print("See ml_pipeline_2.pipeline.hazard_labels module docstring for "
          "the resulting recommendation.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
