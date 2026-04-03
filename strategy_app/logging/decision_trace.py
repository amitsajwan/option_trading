from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from contracts_app import isoformat_ist, normalize_reason_code


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return float(parsed)


def compact_metrics(metrics: Optional[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(metrics, dict):
        return out
    for key, raw in metrics.items():
        value = _safe_float(raw)
        if value is None:
            continue
        out[str(key)] = float(value)
    return out


def normalize_gate(
    gate_id: str,
    *,
    gate_group: str,
    status: str,
    reason_code: Optional[str] = None,
    message: Optional[str] = None,
    metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "gate_id": str(gate_id or "").strip(),
        "gate_group": str(gate_group or "").strip(),
        "status": str(status or "").strip().lower() or "unknown",
        "reason_code": normalize_reason_code(reason_code),
        "message": str(message or "").strip() or None,
        "metrics": compact_metrics(metrics),
    }


def position_state_payload(position: Any) -> dict[str, Any]:
    if position is None:
        return {"has_position": False, "position_id": None}
    return {
        "has_position": True,
        "position_id": str(getattr(position, "position_id", None) or "").strip() or None,
        "signal_id": str(getattr(position, "signal_id", None) or "").strip() or None,
        "direction": str(getattr(position, "direction", None) or "").strip() or None,
        "strike": getattr(position, "strike", None),
        "entry_strategy": str(getattr(position, "entry_strategy", None) or "").strip() or None,
        "entry_regime": str(getattr(position, "entry_regime", None) or "").strip() or None,
        "bars_held": int(getattr(position, "bars_held", 0) or 0),
        "pnl_pct": _safe_float(getattr(position, "pnl_pct", None)),
        "high_water_premium": _safe_float(getattr(position, "high_water_premium", None)),
        "stop_price": _safe_float(getattr(position, "stop_price", None)),
        "trailing_active": bool(getattr(position, "trailing_active", False)),
        "orb_trail_active": bool(getattr(position, "orb_trail_active", False)),
        "oi_trail_active": bool(getattr(position, "oi_trail_active", False)),
    }


def risk_state_payload(risk_manager: Any) -> dict[str, Any]:
    ctx = getattr(risk_manager, "context", None)
    if ctx is None:
        return {}
    return {
        "is_halted": bool(getattr(risk_manager, "is_halted", False)),
        "is_paused": bool(getattr(risk_manager, "is_paused", False)),
        "session_pnl_total": _safe_float(getattr(ctx, "session_pnl_total", None)),
        "session_realised_pnl": _safe_float(getattr(ctx, "session_realised_pnl", None)),
        "session_unrealised_pnl": _safe_float(getattr(ctx, "session_unrealised_pnl", None)),
        "consecutive_losses": int(getattr(ctx, "consecutive_losses", 0) or 0),
        "daily_loss_breached": bool(getattr(ctx, "daily_loss_breached", False)),
        "weekly_loss_breached": bool(getattr(ctx, "weekly_loss_breached", False)),
        "vix_spike_halt": bool(getattr(ctx, "vix_spike_halt", False)),
        "capital_at_risk": _safe_float(getattr(ctx, "capital_at_risk", None)),
    }


def regime_context_payload(regime_signal: Any) -> dict[str, Any]:
    if regime_signal is None:
        return {}
    regime = getattr(regime_signal, "regime", None)
    regime_value = getattr(regime, "value", regime)
    evidence = getattr(regime_signal, "evidence", None)
    return {
        "regime": str(regime_value or "").strip() or None,
        "confidence": _safe_float(getattr(regime_signal, "confidence", None)),
        "reason": str(getattr(regime_signal, "reason", None) or "").strip() or None,
        "evidence": dict(evidence) if isinstance(evidence, dict) else {},
    }


def warmup_context_payload(*, blocked: bool, reason: Optional[str], state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = dict(state) if isinstance(state, dict) else {}
    payload["blocked"] = bool(blocked)
    payload["reason"] = str(reason or "").strip() or None
    return payload


class DecisionTraceBuilder:
    def __init__(
        self,
        *,
        snapshot_id: str,
        timestamp: datetime,
        engine_mode: str,
        decision_mode: str,
        evaluation_type: str,
        run_id: Optional[str] = None,
    ) -> None:
        self._trace: dict[str, Any] = {
            "trace_id": uuid.uuid4().hex[:12],
            "snapshot_id": str(snapshot_id or "").strip() or None,
            "timestamp": isoformat_ist(timestamp),
            "trade_date_ist": isoformat_ist(timestamp)[:10],
            "run_id": str(run_id or "").strip() or None,
            "engine_mode": str(engine_mode or "").strip() or None,
            "decision_mode": str(decision_mode or "").strip() or None,
            "evaluation_type": str(evaluation_type or "").strip() or None,
            "final_outcome": None,
            "primary_blocker_gate": None,
            "selected_candidate_id": None,
            "position_state": {},
            "risk_state": {},
            "regime_context": {},
            "warmup_context": {},
            "summary_metrics": {},
            "flow_gates": [],
            "candidates": [],
        }

    @property
    def trace(self) -> dict[str, Any]:
        return self._trace

    def set_context(
        self,
        *,
        position_state: Optional[dict[str, Any]] = None,
        risk_state: Optional[dict[str, Any]] = None,
        regime_context: Optional[dict[str, Any]] = None,
        warmup_context: Optional[dict[str, Any]] = None,
    ) -> None:
        if isinstance(position_state, dict):
            self._trace["position_state"] = dict(position_state)
        if isinstance(risk_state, dict):
            self._trace["risk_state"] = dict(risk_state)
        if isinstance(regime_context, dict):
            self._trace["regime_context"] = dict(regime_context)
        if isinstance(warmup_context, dict):
            self._trace["warmup_context"] = dict(warmup_context)

    def add_flow_gate(
        self,
        gate_id: str,
        *,
        gate_group: str,
        status: str,
        reason_code: Optional[str] = None,
        message: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        self._trace["flow_gates"].append(
            normalize_gate(
                gate_id,
                gate_group=gate_group,
                status=status,
                reason_code=reason_code,
                message=message,
                metrics=metrics,
            )
        )

    def add_candidate(
        self,
        *,
        strategy_name: Optional[str],
        candidate_type: str,
        direction: Optional[str],
        confidence: Any,
        rank: Optional[int] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        candidate = {
            "candidate_id": uuid.uuid4().hex[:10],
            "strategy_name": str(strategy_name or "").strip() or None,
            "candidate_type": str(candidate_type or "").strip() or None,
            "direction": str(direction or "").strip() or None,
            "confidence": _safe_float(confidence),
            "rank": (int(rank) if rank is not None else None),
            "selected": False,
            "terminal_status": None,
            "terminal_gate_id": None,
            "terminal_reason_code": None,
            "metrics": compact_metrics(metrics),
            "ordered_gates": [],
        }
        self._trace["candidates"].append(candidate)
        return candidate

    def add_candidate_gate(
        self,
        candidate: dict[str, Any],
        gate_id: str,
        *,
        gate_group: str,
        status: str,
        reason_code: Optional[str] = None,
        message: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        candidate["ordered_gates"].append(
            normalize_gate(
                gate_id,
                gate_group=gate_group,
                status=status,
                reason_code=reason_code,
                message=message,
                metrics=metrics,
            )
        )

    def finalize_candidate(
        self,
        candidate: dict[str, Any],
        *,
        terminal_status: str,
        terminal_gate_id: Optional[str] = None,
        terminal_reason_code: Optional[str] = None,
        selected: bool = False,
        extra_metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        candidate["selected"] = bool(selected)
        candidate["terminal_status"] = str(terminal_status or "").strip().lower() or None
        candidate["terminal_gate_id"] = str(terminal_gate_id or "").strip() or None
        candidate["terminal_reason_code"] = normalize_reason_code(terminal_reason_code)
        if isinstance(extra_metrics, dict):
            candidate["metrics"] = {
                **dict(candidate.get("metrics") or {}),
                **compact_metrics(extra_metrics),
            }
        if bool(selected):
            self._trace["selected_candidate_id"] = candidate.get("candidate_id")

    def finalize(
        self,
        *,
        final_outcome: str,
        primary_blocker_gate: Optional[str] = None,
        summary_metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._trace["final_outcome"] = str(final_outcome or "").strip() or None
        self._trace["primary_blocker_gate"] = str(primary_blocker_gate or "").strip() or None
        candidate_count = len(self._trace["candidates"])
        blocked_count = sum(
            1 for item in self._trace["candidates"] if str(item.get("terminal_status") or "").strip().lower() == "blocked"
        )
        selected_count = sum(1 for item in self._trace["candidates"] if bool(item.get("selected")))
        metrics = compact_metrics(summary_metrics)
        metrics.setdefault("candidate_count", float(candidate_count))
        metrics.setdefault("blocked_candidate_count", float(blocked_count))
        metrics.setdefault("selected_candidate_count", float(selected_count))
        self._trace["summary_metrics"] = metrics
        return dict(self._trace)


def build_trace_digest(trace: dict[str, Any]) -> dict[str, Any]:
    candidates = trace.get("candidates") if isinstance(trace.get("candidates"), list) else []
    selected = next((item for item in candidates if isinstance(item, dict) and bool(item.get("selected"))), None)
    blocked_count = sum(
        1
        for item in candidates
        if isinstance(item, dict) and str(item.get("terminal_status") or "").strip().lower() == "blocked"
    )
    return {
        "trace_id": str(trace.get("trace_id") or "").strip() or None,
        "snapshot_id": str(trace.get("snapshot_id") or "").strip() or None,
        "timestamp": str(trace.get("timestamp") or "").strip() or None,
        "trade_date_ist": str(trace.get("trade_date_ist") or "").strip() or None,
        "run_id": str(trace.get("run_id") or "").strip() or None,
        "engine_mode": str(trace.get("engine_mode") or "").strip() or None,
        "decision_mode": str(trace.get("decision_mode") or "").strip() or None,
        "evaluation_type": str(trace.get("evaluation_type") or "").strip() or None,
        "final_outcome": str(trace.get("final_outcome") or "").strip() or None,
        "primary_blocker_gate": str(trace.get("primary_blocker_gate") or "").strip() or None,
        "selected_candidate_id": str(trace.get("selected_candidate_id") or "").strip() or None,
        "selected_candidate": {
            "strategy_name": str(selected.get("strategy_name") or "").strip() or None,
            "direction": str(selected.get("direction") or "").strip() or None,
            "terminal_reason_code": normalize_reason_code(selected.get("terminal_reason_code")),
        }
        if isinstance(selected, dict)
        else None,
        "candidate_count": len(candidates),
        "blocked_candidate_count": blocked_count,
        "position_id": str(((trace.get("position_state") or {}) if isinstance(trace.get("position_state"), dict) else {}).get("position_id") or "").strip() or None,
        "summary_metrics": compact_metrics(trace.get("summary_metrics") if isinstance(trace.get("summary_metrics"), dict) else {}),
    }


__all__ = [
    "DecisionTraceBuilder",
    "build_trace_digest",
    "compact_metrics",
    "normalize_gate",
    "position_state_payload",
    "regime_context_payload",
    "risk_state_payload",
    "warmup_context_payload",
]
