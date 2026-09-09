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

These are meant to run automatically as part of `train` and `backtest`
pipeline nodes and FAIL LOUD (raise) rather than print a warning someone
can miss -- the whole point is not depending on a human remembering to
check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional, Union

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
