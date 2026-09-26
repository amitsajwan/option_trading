"""Hard validation gates for the training/backtest pipeline.

Every check here exists because its absence caused a real incident this
session, not as speculative hardening:

- `validate_backtest_window`: a Feb-Jul 2026 capital-weighted backtest
  overlapped every retrained model's training window (train_end
  2026-06-15) -- a model confirmed structurally broken on its own true
  holdout data (NIFTY's 0.25% target @thr=0.3: precision_fired=0.0,
  negative separation) posted the BEST backtest number of an entire study
  as a result, then collapsed to zero trades on a genuinely clean window.
  See project_backtest_insample_contamination_finding_2026-09-09.md.

- `check_model_degeneracy`: FINNIFTY's live model (finnifty_entry_015pct_v2)
  was deployed with a threshold (0.57) above its own live-observed
  probability ceiling (0.43) -- structurally unable to ever fire, and the
  bug went undetected for the model's entire live history until traced
  back explicitly. A cheap check on the holdout separation table at
  training time would have caught this before it ever reached serving.

- `validate_parquet_instrument`: a SENSEX backtest silently ran against
  NIFTY's own market data and produced a real-looking (wrong, -5.9%
  return, 4 "trades") result, because `run_backtest` never threaded an
  instrument-specific `parquet_base` through to the SIM harness -- every
  instrument fell back to the SAME default snapshot directory, which
  happened to hold NIFTY's data. Caught only because the result looked
  worth double-checking, not because anything failed loudly. See
  project_backtest_wrong_instrument_parquet_2026-09-09.md.

- `check_repricing_wall`: this project has independently found, three
  separate times (a direction-free ATM straddle, a direction-aware debit
  vertical, and a straight-and-faded vertical on a separately retrained
  model -- see `docs/archive/entry_model/MODEL_REBUILD_SPEC_2026-07-11.md`
  Amendment 3/4 and `docs/archive/entry_model/ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md`),
  that buying options AT THE MOMENT a movement/entry model becomes
  confident loses money at every horizon -- "the chain is already
  repriced for the move" by the time detection is confident enough to
  fire. All three prior tests found this out the expensive way, by
  applying a trade unconditionally to every fire and watching it lose. This
  check makes the same test a mandatory, cheap, pre-registered gate: does a
  candidate's apparent edge survive a 1-2 bar delayed entry, or does it
  evaporate/invert exactly like those three prior candidates did.

These are meant to run automatically as part of `train` and `backtest`
pipeline nodes and FAIL LOUD (raise) rather than print a warning someone
can miss -- the whole point is not depending on a human remembering to
check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional, Sequence, Union

DateLike = Union[str, date, datetime]


class BacktestWindowContaminated(ValueError):
    """Raised when a proposed backtest window overlaps the model's own
    training (or valid/holdout) window -- the result would partly measure
    memorization, not genuine forward-looking skill."""


class ModelDegenerate(ValueError):
    """Raised when a trained model's holdout probability distribution is
    unusable at any normal threshold -- e.g. every separation_table entry
    is degenerate, or precision_fired is 0.0 / separation is negative
    wherever the model does fire."""


class ParquetInstrumentMismatch(RuntimeError):
    """Raised when a backtest's parquet_base contains a different
    instrument's snapshot data than the one being backtested."""


class RepricingWallFailure(ValueError):
    """Raised when a candidate's apparent edge at fire-time collapses or
    inverts once entry is delayed 1-2 bars -- the exact failure mode that
    sank three independent prior straddle/vertical experiments (see module
    docstring). A candidate that only "works" at the instant of maximum
    model confidence, and not a bar or two later, is very likely measuring
    the option chain's own repricing of the same signal, not a tradeable
    edge."""


def _to_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


@dataclass(frozen=True)
class WindowValidationResult:
    ok: bool
    backtest_from: date
    backtest_to: date
    holdout_end: date
    gap_days: int
    reason: str = ""


def validate_backtest_window(
    *,
    holdout_end: DateLike,
    backtest_from: DateLike,
    backtest_to: DateLike,
    min_gap_days: int = 0,
    raise_on_fail: bool = True,
) -> WindowValidationResult:
    """Confirm a backtest window starts strictly after the model's
    holdout_end (the latest date the model could have influenced its own
    calibration), with an optional embargo gap for extra safety.

    This is deliberately checked against `holdout_end`, not `train_end` --
    the valid and holdout windows are also used (for isotonic calibration
    and gate evaluation respectively), so a backtest touching those days
    is still measuring data the model's own build process has seen.
    """
    h_end = _to_date(holdout_end)
    b_from = _to_date(backtest_from)
    b_to = _to_date(backtest_to)

    gap_days = (b_from - h_end).days
    ok = gap_days > min_gap_days
    reason = "" if ok else (
        f"backtest_from={b_from} is only {gap_days} day(s) after holdout_end={h_end} "
        f"(need > {min_gap_days}) -- this window would partly overlap data the model's "
        f"own train/valid/holdout split already touched. Pick a backtest window that "
        f"starts strictly after holdout_end, ideally with a real embargo gap."
    )
    result = WindowValidationResult(
        ok=ok, backtest_from=b_from, backtest_to=b_to, holdout_end=h_end,
        gap_days=gap_days, reason=reason,
    )
    if not ok and raise_on_fail:
        raise BacktestWindowContaminated(reason)
    return result


@dataclass(frozen=True)
class DegeneracyReport:
    ok: bool
    usable_thresholds: list[float] = field(default_factory=list)
    best_precision: Optional[float] = None
    best_separation: Optional[float] = None
    holdout_prob_min: Optional[float] = None
    holdout_prob_max: Optional[float] = None
    chosen_threshold: Optional[float] = None
    chosen_threshold_usable: Optional[bool] = None
    reason: str = ""


def check_model_degeneracy(
    holdout_eval: dict[str, Any],
    *,
    chosen_threshold: Optional[float] = None,
    min_precision: float = 0.30,
    min_separation: float = 0.10,
    min_fired_count: int = 20,
    raise_on_fail: bool = True,
) -> DegeneracyReport:
    """Check whether a model's holdout separation_table has ANY threshold
    that's both non-degenerate and actually better than chance (positive
    separation, reasonable precision). A model can pass every ship gate
    (auc/ece/prob_spread) and still be practically unusable -- ship gates
    check aggregate statistics, this checks whether there's an actual
    workable operating point.

    If `chosen_threshold` is given, this ALSO fails when that SPECIFIC
    threshold isn't one of the usable ones -- checking "some threshold
    works" isn't enough on its own, since a caller could still pick a
    different, unusable one from the same table. This is what
    FINNIFTY's live threshold (0.57, above its own live-observed ceiling
    of 0.43) would have failed had this check existed before it deployed.

    `min_fired_count` guards against a second failure mode found
    2026-09-10 alongside the threshold-ceiling incident: once
    `train_entry_dhan_v3.evaluate()`'s threshold grid was widened past its
    old 0.70 cap, several candidates showed a "better" (higher-precision)
    row at a very high threshold that turned out to be based on only
    ~10-20 fired holdout rows -- e.g. BANKNIFTY_015pct's apparent 100%
    precision at thr>=0.80 came from just ~10-21 fires out of 7,383
    holdout rows. That's the same statistical-rigor problem
    `BacktestSummary.is_thin_sample` already guards at the backtest stage
    (<15 live trades), applied here one stage earlier, at threshold
    selection. A row without a `fired_count` field (older bundles, before
    this field existed) is NOT excluded by this check -- only new
    evaluations get the protection, existing usable_thresholds results
    are not retroactively invalidated.

    `holdout_eval` is the dict as embedded in a training bundle
    (`joblib.load(path)["holdout_eval"]`) -- expects `separation_table`
    (list of dicts with thr/fire_rate/precision_fired/separation, or
    {"degenerate": True}) and optionally the raw prob min/max.
    """
    sep_table = holdout_eval.get("separation_table") or []
    prob_min = holdout_eval.get("prob_min")
    prob_max = holdout_eval.get("prob_max")

    usable: list[float] = []
    best_precision = None
    best_separation = None
    for row in sep_table:
        if row.get("degenerate"):
            continue
        precision = row.get("precision_fired")
        separation = row.get("separation")
        if precision is None or separation is None:
            continue
        fired_count = row.get("fired_count")
        if fired_count is not None and fired_count < min_fired_count:
            continue
        if precision >= min_precision and separation >= min_separation:
            usable.append(float(row["thr"]))
        if best_precision is None or precision > best_precision:
            best_precision = precision
        if best_separation is None or separation > best_separation:
            best_separation = separation

    has_any_usable = len(usable) > 0
    chosen_usable = (chosen_threshold in usable) if chosen_threshold is not None else None
    ok = has_any_usable and (chosen_usable is not False)

    if not has_any_usable:
        reason = (
            f"no threshold in the separation_table clears precision>={min_precision} AND "
            f"separation>={min_separation} (best seen: precision={best_precision}, "
            f"separation={best_separation}) -- this model has no usable operating point "
            f"regardless of what its AUC or ship gates say."
        )
    elif chosen_usable is False:
        reason = (
            f"chosen_threshold={chosen_threshold} is NOT one of this model's own usable "
            f"thresholds ({sorted(usable)}) -- do not pick a threshold from a different "
            f"model's table, or one that merely looked good in an earlier version. That "
            f"exact mistake caused FINNIFTY's total live dormancy: its deployed threshold "
            f"(0.57) sat above its own live-observed probability ceiling (0.43)."
        )
    else:
        reason = ""

    result = DegeneracyReport(
        ok=ok, usable_thresholds=sorted(usable), best_precision=best_precision,
        best_separation=best_separation, holdout_prob_min=prob_min,
        holdout_prob_max=prob_max, chosen_threshold=chosen_threshold,
        chosen_threshold_usable=chosen_usable, reason=reason,
    )
    if not ok and raise_on_fail:
        raise ModelDegenerate(reason)
    return result


@dataclass(frozen=True)
class ParquetInstrumentCheck:
    ok: bool
    expected_instrument: str
    actual_instrument: Optional[str]
    reason: str = ""


def validate_parquet_instrument(
    *,
    expected_instrument: str,
    actual_instrument: Optional[str],
    raise_on_fail: bool = True,
) -> ParquetInstrumentCheck:
    """Confirm a parquet_base's snapshot data actually belongs to the
    instrument being backtested, before trusting anything it produces.

    Snapshot rows carry an `instrument` field in the form `"<NAME>FUT"`
    (verified 2026-09-09: BANKNIFTYFUT, NIFTYFUT, FINNIFTYFUT, SENSEXFUT,
    MIDCPNIFTYFUT). `actual_instrument` is whatever a caller read from one
    sample row (see `backtest_runner._read_sample_instrument`, which needs
    duckdb and is only meaningfully callable inside the strategy_app
    container -- this function itself takes the already-read string so
    it stays a pure, unit-testable comparison).
    """
    expected = f"{expected_instrument.strip().upper()}FUT"
    ok = actual_instrument == expected
    reason = "" if ok else (
        f"parquet_base's snapshot data is for instrument={actual_instrument!r}, not the "
        f"expected {expected!r} for this backtest's instrument={expected_instrument!r}. "
        f"Real incident 2026-09-09: a SENSEX backtest silently ran against NIFTY's own "
        f"market data because no instrument-specific parquet_base was configured/checked, "
        f"producing a real-looking but completely wrong result. Fix the parquet_base for "
        f"this instrument (see instrument_config.py) before trusting anything from this run."
    )
    result = ParquetInstrumentCheck(
        ok=ok, expected_instrument=expected_instrument, actual_instrument=actual_instrument, reason=reason,
    )
    if not ok and raise_on_fail:
        raise ParquetInstrumentMismatch(reason)
    return result


@dataclass(frozen=True)
class RepricingWallReport:
    ok: bool
    n_paired_bars: int
    mean_payoff_at_fire: Optional[float] = None
    mean_payoff_delay_1: Optional[float] = None
    mean_payoff_delay_2: Optional[float] = None
    wilcoxon_p_delay_1: Optional[float] = None
    wilcoxon_p_delay_2: Optional[float] = None
    reason: str = ""


def check_repricing_wall(
    *,
    payoff_at_fire: Sequence[Optional[float]],
    payoff_delay_1: Sequence[Optional[float]],
    payoff_delay_2: Sequence[Optional[float]],
    min_paired_bars: int = 20,
    raise_on_fail: bool = True,
) -> RepricingWallReport:
    """Re-score a candidate's fired bars using entry premiums sampled 1 and
    2 bars AFTER the fire instead of at the fire itself (hold-to-exit
    horizon shortened correspondingly by the caller when building these
    sequences -- this function only compares the three already-computed
    payoff distributions, it does not do the re-scoring itself).

    The three sequences must be the SAME fired bars, pairwise aligned by
    index (e.g. `payoff_at_fire[i]`, `payoff_delay_1[i]`, `payoff_delay_2[i]`
    all describe the i-th fire). Only bars where all three are non-None are
    used -- entries are dropped, never imputed, matching every other label
    in this pipeline.

    Fails when the mean payoff at fire-time is positive (there's an
    apparent edge worth testing) but either delayed mean is <= 0 (the edge
    collapsed to break-even-or-worse, or inverted). A candidate with no
    positive edge at fire-time in the first place isn't this check's
    concern -- it should already be failing elsewhere (ship gates,
    degeneracy) for a more direct reason, so this reports ok=True with an
    explanatory reason rather than double-penalizing it.

    `min_paired_bars` guards against drawing a real/inverted-edge
    conclusion from a handful of coincidentally-aligned bars -- mirrors
    `check_model_degeneracy`'s `min_fired_count` guard for the same
    thin-sample-looks-like-a-verdict failure mode.
    """
    triples = [
        (a, b, c)
        for a, b, c in zip(payoff_at_fire, payoff_delay_1, payoff_delay_2)
        if a is not None and b is not None and c is not None
    ]
    n = len(triples)
    if n < min_paired_bars:
        reason = (
            f"only {n} bars have all three (fire/+1/+2) payoffs available, need "
            f">= {min_paired_bars} to draw any conclusion -- this is a thin-sample "
            f"non-result, not evidence either way."
        )
        result = RepricingWallReport(ok=False, n_paired_bars=n, reason=reason)
        if raise_on_fail:
            raise RepricingWallFailure(reason)
        return result

    at_fire = [t[0] for t in triples]
    delay_1 = [t[1] for t in triples]
    delay_2 = [t[2] for t in triples]
    mean_fire = sum(at_fire) / n
    mean_d1 = sum(delay_1) / n
    mean_d2 = sum(delay_2) / n

    p_d1: Optional[float] = None
    p_d2: Optional[float] = None
    try:
        from scipy.stats import wilcoxon

        if any(a != b for a, b in zip(at_fire, delay_1)):
            p_d1 = float(wilcoxon(at_fire, delay_1).pvalue)
        if any(a != c for a, c in zip(at_fire, delay_2)):
            p_d2 = float(wilcoxon(at_fire, delay_2).pvalue)
    except ImportError:
        pass  # significance test is supporting evidence only; the mean-collapse gate below is the real gate

    if mean_fire <= 0:
        reason = (
            f"mean payoff at fire-time is already <= 0 ({mean_fire:.4f}) -- no positive "
            f"edge here to test for repricing-wall collapse; this candidate should be "
            f"failing on other grounds (ship gates / degeneracy), not this check."
        )
        return RepricingWallReport(
            ok=True, n_paired_bars=n, mean_payoff_at_fire=mean_fire,
            mean_payoff_delay_1=mean_d1, mean_payoff_delay_2=mean_d2,
            wilcoxon_p_delay_1=p_d1, wilcoxon_p_delay_2=p_d2, reason=reason,
        )

    collapsed = mean_d1 <= 0 or mean_d2 <= 0
    ok = not collapsed
    reason = "" if ok else (
        f"edge at fire-time (mean={mean_fire:.4f} over {n} bars) collapses to "
        f"mean={mean_d1:.4f} at +1 bar / mean={mean_d2:.4f} at +2 bars -- this is exactly "
        f"the repricing-wall pattern that sank three prior straddle/vertical experiments "
        f"(MODEL_REBUILD_SPEC_2026-07-11.md Amendment 3/4, ENTRY_MODEL_RARELABEL_REBUILD_2026-07-12.md): "
        f"the apparent edge only exists at the exact instant of peak model confidence, which "
        f"means it's very likely measuring the chain's own repricing of the same signal, not "
        f"a tradeable edge. Do not ship this candidate on the strength of its fire-time number alone."
    )
    result = RepricingWallReport(
        ok=ok, n_paired_bars=n, mean_payoff_at_fire=mean_fire,
        mean_payoff_delay_1=mean_d1, mean_payoff_delay_2=mean_d2,
        wilcoxon_p_delay_1=p_d1, wilcoxon_p_delay_2=p_d2, reason=reason,
    )
    if not ok and raise_on_fail:
        raise RepricingWallFailure(reason)
    return result
