from __future__ import annotations

from typing import Any, Optional

from contracts_app import (
    extract_reason_code_from_text,
    merge_decision_metrics,
    normalize_decision_mode,
    normalize_engine_mode,
    normalize_reason_code,
    parse_metric_token,
)

from ..contracts import StrategyVote, TradeSignal


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return float(parsed)


class DecisionFieldResolver:
    def __init__(self) -> None:
        self._context_engine_mode: Optional[str] = None
        self._context_strategy_family_version: Optional[str] = None
        self._context_strategy_profile_id: Optional[str] = None

    def update_context(self, metadata: Optional[dict[str, Any]]) -> None:
        if not isinstance(metadata, dict):
            return
        engine_mode = normalize_engine_mode(metadata.get("engine_mode"))
        if engine_mode is not None:
            self._context_engine_mode = engine_mode
        family = str(metadata.get("strategy_family_version") or "").strip()
        if family:
            self._context_strategy_family_version = family
        profile = str(metadata.get("strategy_profile_id") or "").strip()
        if profile:
            self._context_strategy_profile_id = profile

    def metadata(self, *, run_id: Optional[str], **extra: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if run_id:
            meta["run_id"] = run_id
        if self._context_engine_mode:
            meta["engine_mode"] = self._context_engine_mode
        if self._context_strategy_family_version:
            meta["strategy_family_version"] = self._context_strategy_family_version
        if self._context_strategy_profile_id:
            meta["strategy_profile_id"] = self._context_strategy_profile_id
        for key, value in extra.items():
            if value is not None:
                meta[key] = value
        return meta

    def effective_engine_mode(self, explicit: Any, *, source: Any = None) -> str:
        resolved = normalize_engine_mode(explicit)
        if resolved is not None:
            return resolved
        if str(source or "").strip().upper() == "ML_PURE":
            return "ml_pure"
        if self._context_engine_mode is not None:
            return self._context_engine_mode
        return "deterministic"

    def resolve_decision_mode_for_vote(self, vote: StrategyVote, engine_mode: str) -> str:
        explicit = normalize_decision_mode(vote.decision_mode)
        if explicit is not None:
            return explicit
        if engine_mode == "ml_pure":
            return "ml_staged"
        return "rule_vote"

    def resolve_decision_mode_for_signal(self, signal: TradeSignal, engine_mode: str) -> str:
        explicit = normalize_decision_mode(signal.decision_mode)
        if explicit is not None:
            return explicit
        if engine_mode == "ml_pure":
            return "ml_staged"
        return "rule_vote"

    def resolve_reason_code_for_vote(self, vote: StrategyVote) -> Optional[str]:
        explicit = normalize_reason_code(vote.decision_reason_code)
        if explicit is not None:
            return explicit
        raw_signals = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
        if bool(raw_signals.get("_entry_warmup_blocked")):
            return "entry_warmup_block"
        policy_reason = str(raw_signals.get("_policy_reason") or "").strip()
        if policy_reason:
            if policy_reason.lower().startswith("allowed score="):
                return "policy_allowed"
            return "policy_block"
        return extract_reason_code_from_text(vote.reason)

    def resolve_reason_code_for_signal(self, signal: TradeSignal) -> Optional[str]:
        explicit = normalize_reason_code(signal.decision_reason_code)
        if explicit is not None:
            return explicit
        if signal.exit_reason is not None:
            return normalize_reason_code(signal.exit_reason.value)
        reason = extract_reason_code_from_text(signal.reason)
        if reason is not None:
            return reason
        for vote in signal.votes:
            candidate = self.resolve_reason_code_for_vote(vote)
            if candidate is not None:
                return candidate
        return None

    def resolve_strategy_family_version(
        self,
        *,
        explicit: Any,
        engine_mode: str,
        decision_mode: str,
    ) -> str:
        text = str(explicit or "").strip()
        if text:
            return text
        if self._context_strategy_family_version:
            return self._context_strategy_family_version
        if decision_mode == "ml_staged" or engine_mode == "ml_pure":
            return "ML_PURE_STAGED_V1"
        return "DET_V1"

    def resolve_strategy_profile_id(self, *, explicit: Any, engine_mode: str) -> str:
        text = str(explicit or "").strip()
        if text:
            return text
        if self._context_strategy_profile_id:
            return self._context_strategy_profile_id
        if str(self._context_strategy_family_version or "").strip() == "ML_PURE_STAGED_V1":
            return "ml_pure_staged_v1"
        if engine_mode == "ml_pure":
            return "ml_pure_staged_v1"
        return "det_core_v1"

    def vote_decision_metrics(self, vote: StrategyVote) -> dict[str, float]:
        raw_signals = vote.raw_signals if isinstance(vote.raw_signals, dict) else {}
        policy_score = _safe_float(raw_signals.get("_policy_score"))
        return merge_decision_metrics(
            vote.decision_metrics,
            {
                "confidence": vote.confidence,
                "policy_score": policy_score,
            },
        )

    def signal_decision_metrics(self, signal: TradeSignal) -> dict[str, float]:
        metrics = merge_decision_metrics(signal.decision_metrics, {"confidence": signal.confidence})
        if "ce_prob" not in metrics:
            parsed = parse_metric_token(signal.reason, "ce_prob")
            if parsed is not None:
                metrics["ce_prob"] = parsed
        if "pe_prob" not in metrics:
            parsed = parse_metric_token(signal.reason, "pe_prob")
            if parsed is not None:
                metrics["pe_prob"] = parsed
        if "ce_threshold" not in metrics:
            parsed = parse_metric_token(signal.reason, "ce_thr")
            if parsed is not None:
                metrics["ce_threshold"] = parsed
        if "pe_threshold" not in metrics:
            parsed = parse_metric_token(signal.reason, "pe_thr")
            if parsed is not None:
                metrics["pe_threshold"] = parsed
        if "edge" not in metrics:
            margin = parse_metric_token(signal.reason, "margin")
            if margin is not None:
                metrics["edge"] = margin
            elif "ce_prob" in metrics and "pe_prob" in metrics:
                metrics["edge"] = abs(float(metrics["ce_prob"]) - float(metrics["pe_prob"]))
        return metrics


__all__ = [
    "DecisionFieldResolver",
]
