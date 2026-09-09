"""Config-driven multi-candidate declaration and automated selection --
the "Selection & Decision" node made concrete, instead of a human reading
JSON blobs out of memory files and eyeballing which config won.

This directly codifies MODEL_STUDY_CHECKLIST.md's decision rules, which
were applied BY HAND all through the 2026-09 label-target studies:

  1. A "contaminated" or "degenerate" verdict is a hard exclusion,
     regardless of how good the raw backtest number looks. This is the
     NIFTY 0.25% lesson: it posted the BEST raw return of a 5-config study
     while being genuinely broken (zero real edge, re-scoring its own
     training data) -- see project_backtest_insample_contamination_
     finding_2026-09-09.md. `select_best_candidate` cannot be fooled by
     this the way a human skimming a results table can.
  2. A thin-sample candidate (< min_live_trades) never gets to be the
     confident winner over a robust one, even with a better raw number --
     it can only "win" if EVERY surviving candidate is thin, and even then
     the result is `inconclusive`, not `decided`.

`CandidateConfig` is the declarative half ("config dictates various
models/training configs") -- a plain list of these describes a study
before anything is trained. Actually driving training from this list is
NOT done here: `train_entry_dhan_v3.py` has no CLI knob for label target
percent (that's decided by which `--data-dir` you point it at, built by a
separate, currently ad-hoc data-prep step) -- automating that end-to-end
would mean guessing at unstable tooling. `CandidateConfig` today mainly
documents intent and provides a stable label for
`select_best_candidate`'s output; wiring it to actually launch N training
runs is future work once the data-prep step has a real, committed API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

Decision = Literal["decided", "inconclusive", "no_usable_candidates"]

_HARD_EXCLUDE_VERDICTS = {"contaminated", "degenerate"}


@dataclass(frozen=True)
class CandidateConfig:
    """One trainable/backtestable variant within a study. `algorithm` is
    forward-declared as a field (default "xgboost") so this shape doesn't
    need to change whenever training-time algorithm switching is actually
    built -- see the option-trading skill's note on this being deferred,
    real research scope (different algorithms need different Optuna search
    spaces, not just a different sklearn class)."""

    candidate_id: str
    instrument: str
    label_pct: float
    algorithm: str = "xgboost"
    threshold: Optional[float] = None
    extra_params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def expand_label_grid(
    instrument: str,
    label_pcts: Sequence[float],
    *,
    algorithm: str = "xgboost",
) -> list[CandidateConfig]:
    """Build a grid of candidates varying only label target size -- the
    exact shape of this session's BankNifty/NIFTY label-target studies,
    formalized instead of hand-assembled each time."""
    return [
        CandidateConfig(
            candidate_id=f"{instrument.strip().upper()}_{pct:.4f}pct".rstrip("0").rstrip("."),
            instrument=instrument,
            label_pct=pct,
            algorithm=algorithm,
        )
        for pct in label_pcts
    ]


@dataclass(frozen=True)
class CandidateRanking:
    candidate_id: str
    verdict: Optional[str]
    metric_value: Optional[float]
    is_thin_sample: Optional[bool]
    excluded: bool
    exclusion_reason: Optional[str]


@dataclass(frozen=True)
class SelectionResult:
    decision: Decision
    winner_candidate_id: Optional[str]
    ranked: list[CandidateRanking]
    reasoning: str


def _candidate_id_for(record: dict[str, Any]) -> str:
    config = record.get("config") or {}
    return str(config.get("candidate_id") or config.get("model_path") or "<unknown>")


def select_best_candidate(
    records: Sequence[dict[str, Any]],
    *,
    metric: str = "return_on_capital_pct",
    min_live_trades: int = 15,
) -> SelectionResult:
    """Apply this session's established decision rules to a list of
    backtest registry documents (as returned by
    `RunRegistry.history(node="backtest")`) and pick a winner.

    Pure function -- no Mongo access -- so it's fully unit-testable against
    the exact result shapes this session's real studies produced.
    """
    ranked: list[CandidateRanking] = []
    robust: list[tuple[str, float]] = []
    thin: list[tuple[str, float]] = []

    for record in records:
        candidate_id = _candidate_id_for(record)
        verdict = record.get("verdict")
        result = record.get("result") or {}
        metric_value = result.get(metric)
        is_thin = result.get("is_thin_sample")

        if verdict in _HARD_EXCLUDE_VERDICTS:
            ranked.append(CandidateRanking(
                candidate_id=candidate_id, verdict=verdict, metric_value=metric_value,
                is_thin_sample=is_thin, excluded=True,
                exclusion_reason=f"verdict={verdict} -- hard exclusion regardless of raw metric",
            ))
            continue
        if metric_value is None:
            ranked.append(CandidateRanking(
                candidate_id=candidate_id, verdict=verdict, metric_value=None,
                is_thin_sample=is_thin, excluded=True,
                exclusion_reason=f"result missing metric '{metric}'",
            ))
            continue

        ranked.append(CandidateRanking(
            candidate_id=candidate_id, verdict=verdict, metric_value=float(metric_value),
            is_thin_sample=bool(is_thin), excluded=False, exclusion_reason=None,
        ))
        if is_thin:
            thin.append((candidate_id, float(metric_value)))
        else:
            robust.append((candidate_id, float(metric_value)))

    ranked.sort(key=lambda r: (r.excluded, -(r.metric_value if r.metric_value is not None else float("-inf"))))

    if robust:
        robust.sort(key=lambda pair: pair[1], reverse=True)
        winner = robust[0][0]
        return SelectionResult(
            decision="decided", winner_candidate_id=winner, ranked=ranked,
            reasoning=(
                f"{winner} wins on {metric} ({robust[0][1]:.4g}) among "
                f"{len(robust)} robust (non-thin, non-excluded) candidate(s)."
            ),
        )
    if thin:
        thin.sort(key=lambda pair: pair[1], reverse=True)
        tentative = thin[0][0]
        return SelectionResult(
            decision="inconclusive", winner_candidate_id=tentative, ranked=ranked,
            reasoning=(
                f"No robust (>= {min_live_trades} live trades) candidate survived exclusion. "
                f"{tentative} leads on {metric} ({thin[0][1]:.4g}) among thin-sample candidates "
                f"only -- not enough to distinguish a real result from noise."
            ),
        )
    return SelectionResult(
        decision="no_usable_candidates", winner_candidate_id=None, ranked=ranked,
        reasoning="Every candidate was excluded (contaminated, degenerate, or missing the metric).",
    )
