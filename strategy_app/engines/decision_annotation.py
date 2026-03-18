from __future__ import annotations

from typing import Any, Optional

from contracts_app import normalize_reason_code

from ..contracts import StrategyVote, TradeSignal
from .entry_policy import EntryPolicyDecision


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return float(parsed)


def derive_decision_mode(policy_decision: Optional[EntryPolicyDecision]) -> str:
    checks = dict(policy_decision.checks) if policy_decision is not None else {}
    if any(str(key).startswith("ml_") for key in checks.keys()):
        return "ml_gate"
    return "rule_vote"


def derive_reason_code(policy_decision: Optional[EntryPolicyDecision]) -> str:
    if policy_decision is None:
        return "policy_unknown"
    reason = str(policy_decision.reason or "").strip().lower()
    if reason.startswith("ml:"):
        return "below_threshold" if "<threshold" in reason else "policy_allowed"
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
    checks = raw_signals.get("_policy_checks") if isinstance(raw_signals.get("_policy_checks"), dict) else {}
    policy_reason = str(raw_signals.get("_policy_reason") or "").strip().lower()
    ml_applied = bool(any(str(key).startswith("ml_") for key in checks.keys()) or policy_reason.startswith("ml:"))
    vote.engine_mode = engine_mode
    vote.decision_mode = "ml_gate" if ml_applied else "rule_vote"
    if bool(raw_signals.get("_entry_warmup_blocked")):
        vote.decision_reason_code = "entry_warmup_block"
    elif policy_reason.startswith("allowed score="):
        vote.decision_reason_code = "policy_allowed"
    elif policy_reason.startswith("ml:"):
        vote.decision_reason_code = "below_threshold" if "<threshold" in policy_reason else "policy_allowed"
    elif policy_reason:
        vote.decision_reason_code = "policy_block"
    vote.decision_metrics = {
        "confidence": float(vote.confidence),
        "policy_score": _safe_float(raw_signals.get("_policy_score")),
    }
    vote.strategy_family_version = "ML_GATE_V1" if vote.decision_mode == "ml_gate" else strategy_family_version
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
        mode = "ml_dual" if engine_mode == "ml_pure" else "rule_vote"
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
        if mode == "ml_staged"
        else (
        "ML_GATE_V1"
        if mode == "ml_gate"
        else ("ML_PURE_DUAL_V1" if (mode == "ml_dual" or engine_mode == "ml_pure") else strategy_family_version)
        )
    )
    signal.strategy_profile_id = strategy_profile_id


__all__ = [
    "annotate_signal_contract",
    "annotate_vote_contract",
    "derive_decision_mode",
    "derive_reason_code",
]
