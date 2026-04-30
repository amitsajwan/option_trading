from __future__ import annotations

from typing import Any, Optional

from contracts_app import normalize_reason_code

from ..contracts import StrategyVote, TradeSignal
from ..utils.env import safe_float as _safe_float
from .entry_policy import EntryPolicyDecision


def derive_decision_mode(policy_decision: Optional[EntryPolicyDecision]) -> str:
    return "rule_vote"


def derive_reason_code(policy_decision: Optional[EntryPolicyDecision]) -> str:
    if policy_decision is None:
        return "policy_unknown"
    reason = str(policy_decision.reason or "").strip().lower()
    if reason.startswith("allowed score="):
        return "policy_allowed"
    if reason.startswith("score:"):
        return "policy_block"
    if reason.startswith("timing:"):
        return "timing_block"
    if reason.startswith("momentum:"):
        return "momentum_block"
    if reason.startswith("volume:"):
        return "volume_block"
    if reason.startswith("premium:"):
        return "premium_block"
    if reason.startswith("regime:"):
        return "regime_block"
    return "policy_block"


def annotate_vote_contract(
    vote: StrategyVote,
    *,
    engine_mode: str,
    strategy_family_version: str,
    strategy_profile_id: str,
) -> None:
    raw_signals = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
    policy_reason = str(raw_signals.get("_policy_reason") or "").strip().lower()
    vote.engine_mode = engine_mode
    vote.decision_mode = "rule_vote"
    if bool(raw_signals.get("_entry_warmup_blocked")):
        vote.decision_reason_code = "entry_warmup_block"
    elif policy_reason.startswith("allowed score="):
        vote.decision_reason_code = "policy_allowed"
    elif policy_reason:
        vote.decision_reason_code = "policy_block"
    vote.decision_metrics = {
        "confidence": float(vote.confidence),
        "policy_score": _safe_float(raw_signals.get("_policy_score")),
    }
    vote.strategy_family_version = strategy_family_version
    vote.strategy_profile_id = strategy_profile_id


def annotate_signal_contract(
    signal: TradeSignal,
    *,
    engine_mode: str,
    strategy_family_version: str,
    strategy_profile_id: str,
    decision_mode: Optional[str] = None,
    decision_reason_code: Optional[str] = None,
    decision_metrics: Optional[dict[str, Any]] = None,
) -> None:
    mode = str(decision_mode or "").strip()
    if not mode:
        mode = "ml_staged" if engine_mode == "ml_pure" else "rule_vote"
    signal.engine_mode = engine_mode
    signal.decision_mode = mode
    if decision_reason_code:
        signal.decision_reason_code = normalize_reason_code(decision_reason_code)
    elif signal.exit_reason is not None:
        signal.decision_reason_code = normalize_reason_code(signal.exit_reason.value)
    if isinstance(decision_metrics, dict):
        signal.decision_metrics = dict(decision_metrics)
    elif signal.confidence is not None:
        signal.decision_metrics = {"confidence": float(signal.confidence)}
    signal.strategy_family_version = (
        "ML_PURE_STAGED_V1"
        if (mode == "ml_staged" or engine_mode == "ml_pure")
        else strategy_family_version
    )
    signal.strategy_profile_id = strategy_profile_id


__all__ = [
    "annotate_signal_contract",
    "annotate_vote_contract",
    "derive_decision_mode",
    "derive_reason_code",
]
