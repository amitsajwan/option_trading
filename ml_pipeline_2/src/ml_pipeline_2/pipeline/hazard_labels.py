"""Hazard/duration-to-move entry-timing label (NIFTY entry-focus research,
2026-09-26) -- a survival-analysis reformulation of the existing fixed-
horizon entry label.

``train_entry_dhan_v3.add_labels`` (the current, production-adjacent entry
label) asks a FIXED-HORIZON binary question: "does a qualifying move of
>=threshold happen at all in the next ``horizon_min`` minutes?" That throws
away information about WHEN within (or beyond) that window the move
actually happens -- a bar where the move fires on the very next tick and a
bar where it barely clears the threshold in the very last minute of the
window get the identical label.

This module asks a different question instead: "given we're at bar t, how
long until the next qualifying move?" -- using each bar's own forward price
path as a survival-analysis observation:
  - if a qualifying move happens before the trading session ends that day,
    the observation is UNCENSORED: we know the exact time-to-event.
  - if the session ends first, the observation is RIGHT-CENSORED: we only
    know the true time-to-event is *at least* however many bars were left
    in the session -- not that no move was ever coming.

This is deliberately NOT a re-run of either prior direction-agnostic "is a
move coming" attempt on this project: the compression-state-machine
(``docs/archive/entry_model/COMPRESSION_STATE_ENGINE_PLAN.md``, real but
modest 1.26x lift, failed its own pre-registered 1.5x gate) and the
compression-feature A/B on the existing classifier
(``docs/archive/entry_model/BMM_RESULTS.md``, +0.009 AUC on a research view
but -0.016 on the clean production-parity view). Neither of those used a
duration/hazard target; this module's contribution is the reformulation
itself -- a continuous "how imminent" score instead of one fixed-horizon
flag -- not a new feature set or a new rule engine.

Session-window handling is reused, not re-derived, from
``ml_pipeline_2.scripts.train_entry_dhan_v3``: the same
``SESSION_START_MIN``/``SESSION_END_MIN`` constants (9:45-15:05 IST) gate
which bars get a label, and the same day-sorted (``trade_date``,
``timestamp``) structure is used so a per-day walk-forward scan can never
leak across a day boundary (naive global rolling, as a fixed-horizon
rolling-window label can get away with, would leak the first bars of day
N+1 into day N's tail -- this module groups per ``trade_date`` explicitly
to rule that out by construction).

Objective-function spike (see ``ml_pipeline_2/scripts/
spike_hazard_objective.py`` for the runnable comparison -- run it yourself
with ``python -m ml_pipeline_2.scripts.spike_hazard_objective`` to see fresh
numbers) -- RECOMMENDATION: use XGBoost's native ``survival:cox`` objective
for this project, NOT the discrete time-bucket reformulation that looked
like the safer default going in. This reverses an initial hunch (a plain
multiclass classifier needs no new DMatrix-level label-encoding API, so it
looked like the "boring, easy to integrate" choice) -- the actual spike
numbers say otherwise, across 5 different random seeds
(``--seeds 42 7 123 2026 99``, the default):

  ```
  objective         mean_c_index    mean_gap   wins(c_index)   wins(gap mag)
  survival:cox            0.6905    -10.3090               4               5
  survival:aft            0.6868     -4.6726               1               0
  discrete_bucket         0.6820     -2.8485               0               0
  ```
  (`gap` = mean predicted-time for the synthetic high-hazard regime minus
  the low-hazard regime -- designed to be strongly negative; `wins` = how
  many of the 5 seeds that objective had the best value.)

``survival:cox`` had the best concordance in 4/5 seeds and the best
ground-truth regime separation in all 5 -- not a photo finish. The reason,
found while building the spike's more realistic synthetic censoring
(variable per-row censor times, matching how a real bar's "bars left until
session end" varies -- an earlier, lazier version of the synthetic data
used one fixed censor time for every row and hid this entirely): the
discrete-bucket approach must DROP any censored row whose censor time
falls short of the last bucket edge, because its true bucket is genuinely
ambiguous (see ``spike_hazard_objective.py``'s ``_bucket_labels``). In the
5-seed run above that dropped ~370-383 of 1400 training rows each time
(~27%) -- both survival objectives use every row's partial information
natively via their censored-likelihood machinery and lose nothing. That
information loss, not label-encoding elegance, is what decided this.

None of this makes ``survival:cox`` free of gotchas -- if anything it has
the sharpest one, confirmed by reading XGBoost's own objective source
(``src/objective/regression_obj.cu``, ``CoxRegression::GetGradient``), not
assumed:

1. The label's sign encodes observed-vs-censored (negative = right-
   censored, confirmed against XGBoost's own parameter docs), but the
   observed/censored indicator in the actual gradient computation is
   ``label > 0`` -- meaning a label of exactly ``+0.0`` (or ``-0.0``;
   IEEE754 does not distinguish them under ``>``) is ALWAYS treated as
   censored, silently, regardless of sign or intent. This module's own
   ``event_observed=False`` bars CAN legitimately have
   ``time_to_event == 0`` (a bar at the very last row of the session --
   zero bars remained to look forward into) -- which happens to already be
   consistent with Cox's forced-censored-at-zero behavior, but that is a
   coincidence of this specific label, not a general safety net. Anyone
   wiring this module's output into ``survival:cox`` should still shift
   times by +1 (or otherwise special-case zero) defensively rather than
   rely on that coincidence holding for every future variant of this label.
2. ``predict()`` returns a risk score (``exp(raw_margin)``, confirmed from
   the same source file's ``PredTransform``), not a time -- every
   downstream consumer (``concordance_index``, the bucket-calibration
   check in ``evaluate_hazard``, any future dashboard) must remember to
   negate/invert it first. ``survival:aft``'s ``predict()`` is directly
   comparable to the training time bounds with no inversion (confirmed via
   XGBoost's own AFT demo script, which prints predictions next to
   ``label_lower_bound``/``label_upper_bound`` unmodified) -- a real
   usability edge for AFT, just not enough to overcome its extra
   ``aft_loss_distribution``/``aft_loss_distribution_scale`` tuning
   surface and its narrower (though still real, seed-4/5) margin over Cox.

Net call: ``survival:cox``, with the sign/zero-time handling made explicit
and unit-tested wherever this label is wired into a real training script --
not left as an implicit assumption the way the task brief warned it's easy
to get backwards. The discrete-bucket reformulation is not wrong, and
remains the simpler fallback if a future maintainer wants to avoid
DMatrix-level label plumbing entirely (e.g. to reuse ``train_entry_dhan_v3.
run_hpo``'s Optuna machinery unmodified) -- just go in knowing it is
measurably leaving real signal on the table by discarding ambiguously-
censored rows, not merely "simpler for free."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from ml_pipeline_2.scripts.train_entry_dhan_v3 import (
    LABEL_PT_THRESHOLD,
    SESSION_END_MIN,
    SESSION_START_MIN,
)

log = logging.getLogger("hazard_labels")


# ── Label construction ──────────────────────────────────────────────────────

def add_hazard_labels(
    df: pd.DataFrame,
    *,
    label_pct: Optional[float] = None,
    label_pt: Optional[float] = None,
) -> pd.DataFrame:
    """Add ``time_to_event``/``event_observed`` duration-to-move labels.

    For every bar `t` inside the 9:45-15:05 IST session window (same
    ``SESSION_START_MIN``/``SESSION_END_MIN`` gate as
    ``train_entry_dhan_v3.add_labels``), walks forward bar-by-bar tracking
    the running max upward and downward excursion from ``close[t]`` --
    the exact same "max of up-move/down-move so far" construction
    ``add_labels`` uses, just not collapsed into one fixed horizon -- until
    either excursion first crosses the move threshold, or the day's bars
    run out (the trading session ends).

    ``time_to_event`` is the smallest ``k >= 1`` (in bars) at which the
    threshold was crossed (``event_observed=True``), or the number of bars
    remaining until the day's last row if the threshold was never crossed
    (``event_observed=False`` -- right-censored: the true time-to-event is
    *at least* this many bars, not "no move was ever coming"). Bars outside
    the session window get ``NaN``/``NaN`` for both, mirroring
    ``add_labels``'s own out-of-window ``NaN`` handling -- filter with
    ``df["event_observed"].notna()`` before using.

    Exactly one of `label_pct` (percent of that bar's own close -- the
    instrument/price-level-agnostic convention, matches
    ``add_labels(label_pct=...)``) or `label_pt` (fixed raw points) should
    be given; if both are None, falls back to
    ``train_entry_dhan_v3.LABEL_PT_THRESHOLD`` for the same
    backward-compatible default `add_labels` uses.

    Day-boundary safety: the DataFrame is sorted by
    ``["trade_date", "timestamp"]`` and the walk-forward scan is run
    per-``trade_date`` group -- a bar near the end of one day can NEVER see
    the next day's bars, however far its own day's data happens to extend
    (a naive global rolling window over the whole sorted frame, as a
    fixed-horizon label can get away with when the horizon is short enough
    to rarely span midnight, would not have this guarantee).

    Performance note: this is an O(bars-until-crossion-or-session-end) scan
    per labelable bar (worst case O(n) per bar within a single day, when no
    crossing ever happens), not a vectorized rolling computation --
    reasonable for a research spike on a day's or a month's worth of
    1-minute bars, but a full multi-year 1-minute NIFTY history should be
    profiled before assuming this scales; a vectorized reformulation is
    possible but not implemented here.
    """
    if label_pct is not None and label_pt is not None:
        raise ValueError("add_hazard_labels: pass at most one of label_pct / label_pt, not both")
    df = df.sort_values(["trade_date", "timestamp"]).reset_index(drop=True).copy()
    close_col = next((c for c in ["px_fut_close", "close", "fut_close"] if c in df.columns), None)
    if close_col is None:
        raise ValueError(f"No close price column found. Columns: {list(df.columns[:20])}")

    def threshold_for(c0: float) -> float:
        if label_pct is not None:
            return abs(c0) * (label_pct / 100.0)
        return float(label_pt) if label_pt is not None else float(LABEL_PT_THRESHOLD)

    n = len(df)
    time_to_event = np.full(n, np.nan, dtype=float)
    event_observed = np.full(n, np.nan, dtype=float)

    for _, day_df in df.groupby("trade_date", sort=False):
        positions = day_df.index.to_numpy()
        closes = pd.to_numeric(day_df[close_col], errors="coerce").to_numpy(dtype=float)
        m = len(closes)
        tte_day = np.full(m, np.nan, dtype=float)
        obs_day = np.full(m, np.nan, dtype=float)
        for t in range(m):
            c0 = closes[t]
            if not np.isfinite(c0):
                continue
            threshold = threshold_for(c0)
            up_excursion = 0.0
            down_excursion = 0.0
            crossed_at: Optional[int] = None
            for k in range(1, m - t):
                ck = closes[t + k]
                if not np.isfinite(ck):
                    continue
                move_up = ck - c0
                if move_up > up_excursion:
                    up_excursion = move_up
                move_down = c0 - ck
                if move_down > down_excursion:
                    down_excursion = move_down
                if max(up_excursion, down_excursion) >= threshold:
                    crossed_at = k
                    break
            if crossed_at is not None:
                tte_day[t] = float(crossed_at)
                obs_day[t] = 1.0
            else:
                # Right-censored: the session (this day's data) ran out
                # before a qualifying move crossed threshold. The bars
                # remaining until the day's last row is a lower bound on
                # the true time-to-event, not a real event time.
                tte_day[t] = float(m - 1 - t)
                obs_day[t] = 0.0
        time_to_event[positions] = tte_day
        event_observed[positions] = obs_day

    df["time_to_event"] = time_to_event
    df["event_observed"] = event_observed

    ts_col = df["timestamp"] if "timestamp" in df.columns else pd.to_datetime(df.index)
    minute_of_day = pd.to_datetime(ts_col).dt.hour * 60 + pd.to_datetime(ts_col).dt.minute
    outside = ((minute_of_day < SESSION_START_MIN) | (minute_of_day > SESSION_END_MIN)).to_numpy()
    df.loc[outside, "time_to_event"] = np.nan
    df.loc[outside, "event_observed"] = np.nan
    log.info("Session filter 9:45-15:05: excluded %d bars outside window", int(outside.sum()))

    valid = df["event_observed"].notna()
    if valid.any():
        log.info(
            "Hazard labels: %d valid bars, event_rate=%.3f, median_time_to_event=%.1f bars",
            int(valid.sum()),
            float(df.loc[valid, "event_observed"].mean()),
            float(df.loc[valid, "time_to_event"].median()),
        )
    return df


# ── Evaluation ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConcordanceResult:
    """Harrell's concordance index (C-index) -- the survival-analysis
    analog of AUC: the fraction of COMPARABLE pairs where the model
    correctly orders which one's event happens sooner. 0.5 = no better
    than chance, 1.0 = perfect ordering."""

    c_index: float
    n_comparable: int
    n_concordant: int
    n_discordant: int
    n_tied_prediction: int


def _pair_order(t_a: float, e_a: bool, t_b: float, e_b: bool) -> Optional[str]:
    """Which of a/b has the definitely-earlier TRUE event time, or None if
    the pair is not comparable (we cannot know the true order).

    - both observed: comparable iff their times differ; order by time.
    - one observed, one censored: comparable only if the OBSERVED time is
      strictly less than the CENSORED time -- then the observed one is
      definitely first (the censored one's true event is >= its censor
      time, which is already later). If the observed time is >= the
      censored time, we cannot tell (the censored one's true event could
      be earlier or later than the observed one) -- not comparable.
    - both censored: never comparable (no information about true order).
    """
    if e_a and e_b:
        if t_a < t_b:
            return "a_first"
        if t_b < t_a:
            return "b_first"
        return None  # exact tie in true time -- no determinable order
    if e_a and not e_b:
        return "a_first" if t_a < t_b else None
    if e_b and not e_a:
        return "b_first" if t_b < t_a else None
    return None  # both censored


def concordance_index(
    predicted_time: Sequence[float],
    actual_time: Sequence[float],
    event_observed: Sequence[bool],
) -> ConcordanceResult:
    """Harrell's C-index, implemented as a straightforward pairwise
    comparison over all comparable pairs (see `_pair_order`) -- no new
    dependency, `lifelines`/`scikit-survival` are not installed in this
    repo's venv (checked before writing this).

    `predicted_time` must use the convention "lower predicted value = the
    model thinks the event happens SOONER" -- the natural convention for a
    duration/hazard-bucket model's own output. A model whose native output
    is a RISK SCORE where higher = sooner (e.g. XGBoost's `survival:cox`,
    whose `predict()` returns `exp(risk)`) must be negated (or otherwise
    inverted) by the caller before being passed in here.

    O(n^2) pairwise comparison -- fine for a research-scale evaluation set
    (hundreds to a few thousand rows); would need a sorted-order O(n log n)
    algorithm (as `lifelines`/`scikit-survival` implement) before running
    this against a full multi-year holdout set with tens of thousands of
    rows.
    """
    p = np.asarray(predicted_time, dtype=float)
    t = np.asarray(actual_time, dtype=float)
    e = np.asarray(event_observed, dtype=bool)
    n = len(p)
    if not (len(t) == n and len(e) == n):
        raise ValueError("predicted_time, actual_time, event_observed must all be the same length")

    concordant = 0
    discordant = 0
    tied_pred = 0
    comparable = 0
    for i in range(n):
        for j in range(i + 1, n):
            order = _pair_order(float(t[i]), bool(e[i]), float(t[j]), bool(e[j]))
            if order is None:
                continue
            comparable += 1
            if order == "a_first":
                sooner_p, later_p = p[i], p[j]
            else:
                sooner_p, later_p = p[j], p[i]
            if sooner_p < later_p:
                concordant += 1
            elif sooner_p > later_p:
                discordant += 1
            else:
                tied_pred += 1

    c_index = (
        (concordant + 0.5 * tied_pred) / comparable if comparable > 0 else float("nan")
    )
    return ConcordanceResult(
        c_index=float(c_index),
        n_comparable=comparable,
        n_concordant=concordant,
        n_discordant=discordant,
        n_tied_prediction=tied_pred,
    )


@dataclass(frozen=True)
class HazardBucket:
    bucket_index: int
    predicted_time_range: tuple[float, float]
    n_rows: int
    n_observed: int
    realized_median_time: Optional[float]  # median actual_time among OBSERVED rows only; None if none


@dataclass(frozen=True)
class HazardEvaluation:
    concordance: ConcordanceResult
    buckets: list[HazardBucket] = field(default_factory=list)
    monotonic: Optional[bool] = None  # None if fewer than 2 buckets have a defined median to compare


def evaluate_hazard(
    predicted_time: Sequence[float],
    actual_time: Sequence[float],
    event_observed: Sequence[bool],
    *,
    n_buckets: int = 3,
) -> HazardEvaluation:
    """C-index plus a basic calibration/monotonicity check: bucket bars by
    predicted-time (ascending -- bucket 0 is "predicted soonest"), and
    confirm the realized median time-to-event among each bucket's OBSERVED
    rows is non-decreasing across buckets. This is deliberately NOT a full
    calibration curve (per the task brief) -- a bucket with zero observed
    rows gets `realized_median_time=None` and is skipped in the
    monotonicity check rather than breaking it.

    Note: `realized_median_time` is computed over OBSERVED (uncensored)
    rows only, not a censoring-aware (e.g. Kaplan-Meier) median -- a
    simplification appropriate for "a simple monotonicity check, not a
    full calibration curve"; a bucket dominated by censored rows will have
    a `realized_median_time` biased toward its shorter-lived observed
    rows only. Fine for validating a synthetic spike with known ground
    truth; revisit before trusting this on a heavily-censored real slice.
    """
    p = np.asarray(predicted_time, dtype=float)
    t = np.asarray(actual_time, dtype=float)
    e = np.asarray(event_observed, dtype=bool)
    n = len(p)
    if not (len(t) == n and len(e) == n):
        raise ValueError("predicted_time, actual_time, event_observed must all be the same length")
    if n_buckets < 1:
        raise ValueError("n_buckets must be >= 1")

    conc = concordance_index(p, t, e)

    order = np.argsort(p, kind="mergesort")
    edges = np.linspace(0, n, n_buckets + 1).astype(int)
    buckets: list[HazardBucket] = []
    medians: list[Optional[float]] = []
    for b in range(n_buckets):
        lo, hi = edges[b], edges[b + 1]
        rows = order[lo:hi]
        if len(rows) == 0:
            buckets.append(HazardBucket(b, (float("nan"), float("nan")), 0, 0, None))
            medians.append(None)
            continue
        p_range = (float(p[rows].min()), float(p[rows].max()))
        obs_rows = rows[e[rows]]
        median = float(np.median(t[obs_rows])) if len(obs_rows) > 0 else None
        buckets.append(HazardBucket(b, p_range, len(rows), len(obs_rows), median))
        medians.append(median)

    defined = [m for m in medians if m is not None]
    if len(defined) < 2:
        monotonic: Optional[bool] = None
    else:
        monotonic = all(defined[i] <= defined[i + 1] for i in range(len(defined) - 1))

    return HazardEvaluation(concordance=conc, buckets=buckets, monotonic=monotonic)
