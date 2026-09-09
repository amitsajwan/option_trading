"""The unattended, sequential, multi-instrument orchestrator: "train all
instruments, go to sleep, come back and see what happened, why, and which
model won" made real, instead of a human running one train_entry_dhan_v3
invocation at a time and copying results into a memory file by hand.

Sequential by design, not parallel -- this session found XGBoost's own
internal OpenMP parallelism means "N processes at once" on this VM's 4
cores is N-way oversubscription, not N-way speedup (280-330s/trial under
3-way contention vs 15-70s/trial decontended). One candidate at a time,
one instrument at a time, is not just simpler, it is measurably faster
wall-clock too.

Every function here is pure or takes its I/O (train_fn, backtest_fn,
registry) as injected dependencies, so the actual sequencing/error-
isolation/summary logic is fully unit-testable without a real VM, Mongo,
or hours of XGBoost training -- the same pattern backtest_runner.py's
run_range_fn injection already established.

Design decisions, made explicitly rather than left implicit:
  - One failure (bad data, a training crash, a degenerate model) must
    never stop the rest of the run -- every candidate and every
    instrument is wrapped so a partial run still produces a usable
    summary for everything that DID complete.
  - This module trains, backtests, records, and selects. It NEVER
    deploys or touches a live container -- that stays a separate,
    explicit, human-approved step (see the option-trading skill's
    Deployment process and this repo's standing BankNifty/NIFTY halt).
  - The backtest window is NOT auto-derived from the training window.
    Getting that window right (clean of the training/holdout period) was
    this session's single biggest incident
    (project_backtest_insample_contamination_finding_2026-09-09.md) --
    it stays an explicit, deliberate input to CandidatePlan rather than a
    formula this module could get subtly wrong for every candidate at
    once. `backtest_runner.run_backtest` still enforces
    `validate_backtest_window` as the hard safety net regardless.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Optional, Sequence

import pandas as pd

from .candidates import select_best_candidate, SelectionResult

StageReached = Literal[
    "train_failed", "trained_degenerate", "backtest_skipped_no_threshold",
    "backtest_failed", "backtest_recorded",
]

TrainFn = Callable[..., dict[str, Any]]
BacktestFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class CandidatePlan:
    """One candidate to train + backtest within an instrument's study."""

    candidate_id: str
    label_pct: float
    train_start: str
    train_end: str
    valid_start: str
    valid_end: str
    holdout_start: str
    holdout_end: str
    backtest_from: str
    backtest_to: str
    n_trials: int = 60
    min_gap_days: int = 0


@dataclass(frozen=True)
class InstrumentStudyPlan:
    instrument: str
    study_id: str
    data_dir: str
    output_dir: str
    candidates: Sequence[CandidatePlan]


def shrink_plan_for_dry_run(
    plan: InstrumentStudyPlan,
    *,
    max_days: int = 5,
    n_trials: int = 3,
) -> InstrumentStudyPlan:
    """Return a copy of `plan` with every candidate's date windows clipped
    to `max_days` calendar days each (train/valid/holdout/backtest, chained
    back-to-back starting from the candidate's own original train_start)
    and HPO trials cut to `n_trials`.

    For quickly proving the whole pipeline runs end-to-end -- data load,
    label computation, HPO, calibration, holdout eval, ship-gate check,
    backtest window validation, backtest execution, registry recording,
    selection -- in minutes instead of hours, before trusting it for a
    real unattended overnight run. Statistically meaningless by design
    (5 days of data proves nothing about a model's real edge); it only
    proves nothing crashes. Anchored at each candidate's own original
    train_start (not "today" or holdout_end) so it stays safely inside
    whatever historical range that instrument's data_dir actually has,
    regardless of how recently the instrument was onboarded.
    """
    def _shrink(c: CandidatePlan) -> CandidatePlan:
        train_start = pd.Timestamp(c.train_start)
        train_end = train_start + pd.Timedelta(days=max_days)
        valid_start = train_end + pd.Timedelta(days=1)
        valid_end = valid_start + pd.Timedelta(days=max_days)
        holdout_start = valid_end + pd.Timedelta(days=1)
        holdout_end = holdout_start + pd.Timedelta(days=max_days)
        backtest_from = holdout_end + pd.Timedelta(days=1)
        backtest_to = backtest_from + pd.Timedelta(days=max_days)
        fmt = lambda ts: ts.strftime("%Y-%m-%d")  # noqa: E731
        return replace(
            c,
            train_start=fmt(train_start), train_end=fmt(train_end),
            valid_start=fmt(valid_start), valid_end=fmt(valid_end),
            holdout_start=fmt(holdout_start), holdout_end=fmt(holdout_end),
            backtest_from=fmt(backtest_from), backtest_to=fmt(backtest_to),
            n_trials=n_trials,
        )

    return replace(plan, candidates=[_shrink(c) for c in plan.candidates])


@dataclass(frozen=True)
class CandidateOutcome:
    candidate_id: str
    label_pct: float
    stage_reached: StageReached
    model_path: Optional[str]
    threshold_used: Optional[float]
    train_result: Optional[dict[str, Any]]
    backtest_result: Optional[dict[str, Any]]
    error: Optional[str]


@dataclass(frozen=True)
class InstrumentStudyOutcome:
    instrument: str
    study_id: str
    candidates: list[CandidateOutcome]
    selection: Optional[SelectionResult]
    fatal_error: Optional[str]


def _best_threshold_from_holdout_eval(holdout_eval: dict[str, Any]) -> Optional[float]:
    """Pick the single usable threshold with the highest separation from a
    model's own separation_table -- the automatic version of what a human
    reads off the table by eye before choosing what to backtest."""
    table = holdout_eval.get("separation_table") or []
    usable = [row for row in table if not row.get("degenerate") and row.get("separation") is not None]
    if not usable:
        return None
    best = max(usable, key=lambda row: row["separation"])
    return float(best["thr"])


def run_candidate(
    plan: CandidatePlan,
    *,
    instrument: str,
    study_id: str,
    data_dir: str,
    output_dir: str,
    train_fn: TrainFn,
    backtest_fn: BacktestFn,
    registry: Any,
) -> CandidateOutcome:
    """Train one candidate, then backtest it if (and only if) training
    produced a non-degenerate model with a usable threshold. Never raises
    -- every failure mode is captured in the returned CandidateOutcome so
    the caller can keep going to the next candidate regardless."""
    model_path = f"{output_dir.rstrip('/')}/{plan.candidate_id}.joblib"

    try:
        train_result = train_fn(
            instrument=instrument, data_dir=data_dir, output=model_path,
            train_start=plan.train_start, train_end=plan.train_end,
            valid_start=plan.valid_start, valid_end=plan.valid_end,
            holdout_start=plan.holdout_start, holdout_end=plan.holdout_end,
            n_trials=plan.n_trials, label_pct=plan.label_pct,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: isolate one candidate's failure
        return CandidateOutcome(
            candidate_id=plan.candidate_id, label_pct=plan.label_pct, stage_reached="train_failed",
            model_path=None, threshold_used=None, train_result=None, backtest_result=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    registry.record(_train_document(instrument, study_id, plan, train_result, model_path))

    holdout_eval = train_result.get("holdout_eval") or {}
    threshold = _best_threshold_from_holdout_eval(holdout_eval)
    if threshold is None:
        return CandidateOutcome(
            candidate_id=plan.candidate_id, label_pct=plan.label_pct, stage_reached="trained_degenerate",
            model_path=model_path, threshold_used=None, train_result=train_result, backtest_result=None,
            error="no usable (non-degenerate) threshold in this model's own separation_table",
        )

    try:
        backtest_result = backtest_fn(
            instrument=instrument, model_path=model_path, entry_min_prob=threshold,
            date_from=plan.backtest_from, date_to=plan.backtest_to,
            holdout_end=plan.holdout_end, holdout_eval=holdout_eval,
            min_gap_days=plan.min_gap_days,
            label=f"{plan.candidate_id} @thr={threshold}",
        )
    except Exception as exc:  # noqa: BLE001
        return CandidateOutcome(
            candidate_id=plan.candidate_id, label_pct=plan.label_pct, stage_reached="backtest_failed",
            model_path=model_path, threshold_used=threshold, train_result=train_result, backtest_result=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    verdict = "inconclusive" if backtest_result.get("is_thin_sample") else (
        "usable" if (backtest_result.get("live_trades") or 0) > 0 else "degenerate"
    )
    registry.record(_backtest_document(instrument, study_id, plan, threshold, backtest_result, model_path, verdict))

    return CandidateOutcome(
        candidate_id=plan.candidate_id, label_pct=plan.label_pct, stage_reached="backtest_recorded",
        model_path=model_path, threshold_used=threshold, train_result=train_result,
        backtest_result=backtest_result, error=None,
    )


def _train_document(instrument: str, study_id: str, plan: CandidatePlan, train_result: dict, model_path: str) -> dict:
    from .registry import build_run_document
    gates_pass = bool(train_result.get("ship_gates_all_pass"))
    return build_run_document(
        instrument=instrument, study_id=study_id, node="train",
        config={
            "candidate_id": plan.candidate_id, "label_pct": plan.label_pct,
            "train_window": f"{plan.train_start}..{plan.train_end}",
            "holdout_window": f"{plan.holdout_start}..{plan.holdout_end}",
            "n_trials": plan.n_trials,
        },
        result={
            "holdout_eval": train_result.get("holdout_eval"),
            "ship_gates": train_result.get("ship_gates"),
            "ship_gates_all_pass": gates_pass,
        },
        verdict="usable" if gates_pass else "degenerate",
        artifact_path=model_path,
    )


def _backtest_document(instrument: str, study_id: str, plan: CandidatePlan, threshold: float,
                        backtest_result: dict, model_path: str, verdict: str) -> dict:
    from .registry import build_run_document
    return build_run_document(
        instrument=instrument, study_id=study_id, node="backtest",
        config={
            "candidate_id": plan.candidate_id, "model_path": model_path, "threshold": threshold,
            "date_from": plan.backtest_from, "date_to": plan.backtest_to, "holdout_end": plan.holdout_end,
        },
        result=backtest_result, verdict=verdict, artifact_path=model_path,
    )


def run_instrument_study(
    plan: InstrumentStudyPlan,
    *,
    train_fn: TrainFn,
    backtest_fn: BacktestFn,
    registry: Any,
) -> InstrumentStudyOutcome:
    """Run every candidate in one instrument's study sequentially, then
    select a winner from whatever backtest records resulted. Isolates a
    total failure of the instrument itself (e.g. missing data_dir) so it
    can't take down a multi-instrument overnight run."""
    outcomes: list[CandidateOutcome] = []
    try:
        for candidate_plan in plan.candidates:
            outcomes.append(run_candidate(
                candidate_plan, instrument=plan.instrument, study_id=plan.study_id,
                data_dir=plan.data_dir, output_dir=plan.output_dir,
                train_fn=train_fn, backtest_fn=backtest_fn, registry=registry,
            ))

        backtest_records = registry.history(instrument=plan.instrument, study_id=plan.study_id, node="backtest")
        selection = select_best_candidate(backtest_records) if backtest_records else None
        if selection is not None:
            from .registry import build_run_document
            registry.record(build_run_document(
                instrument=plan.instrument, study_id=plan.study_id, node="deploy_decision",
                config={}, result={
                    "decision": selection.decision, "winner_candidate_id": selection.winner_candidate_id,
                    "ranked": [r.__dict__ for r in selection.ranked],
                },
                verdict=selection.decision if selection.decision == "decided" else "inconclusive",
                notes=selection.reasoning,
            ))
    except Exception as exc:  # noqa: BLE001 -- isolate one instrument's total failure so a
                              # multi-instrument overnight run can't die on instrument #2 of 5.
        return InstrumentStudyOutcome(
            instrument=plan.instrument, study_id=plan.study_id, candidates=outcomes,
            selection=None, fatal_error=f"{type(exc).__name__}: {exc}",
        )

    return InstrumentStudyOutcome(
        instrument=plan.instrument, study_id=plan.study_id, candidates=outcomes,
        selection=selection, fatal_error=None,
    )


def run_all_instruments(
    plans: Sequence[InstrumentStudyPlan],
    *,
    train_fn: TrainFn,
    backtest_fn: BacktestFn,
    registry: Any,
) -> list[InstrumentStudyOutcome]:
    """Run every instrument's study sequentially. This is the one function
    an unattended overnight run actually calls."""
    return [
        run_instrument_study(plan, train_fn=train_fn, backtest_fn=backtest_fn, registry=registry)
        for plan in plans
    ]


def format_summary(outcomes: Sequence[InstrumentStudyOutcome]) -> str:
    """Human-readable "what happened, why, which model" report -- the
    thing to read first after an overnight run, before querying the
    registry for more detail."""
    lines: list[str] = []
    for outcome in outcomes:
        lines.append(f"\n=== {outcome.instrument} / {outcome.study_id} ===")
        if outcome.fatal_error:
            lines.append(f"  FATAL: {outcome.fatal_error}")
            continue
        for c in outcome.candidates:
            status = c.stage_reached
            detail = ""
            if c.stage_reached == "backtest_recorded" and c.backtest_result:
                r = c.backtest_result
                detail = (f" return={r.get('return_on_capital_pct')} "
                          f"win_rate={r.get('win_rate_pct')} live_trades={r.get('live_trades')} "
                          f"thin={r.get('is_thin_sample')}")
            elif c.error:
                detail = f" ({c.error})"
            lines.append(f"  {c.candidate_id:30s} label_pct={c.label_pct:<7} {status}{detail}")
        if outcome.selection is None:
            lines.append("  No backtest records -> no selection possible.")
        else:
            s = outcome.selection
            lines.append(f"  DECISION: {s.decision} -- winner={s.winner_candidate_id}")
            lines.append(f"  REASON: {s.reasoning}")
    return "\n".join(lines)
