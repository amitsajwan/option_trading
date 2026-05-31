"""Canonical event contracts for all 6 strategy decision stages.

Every event carries the 8 required metadata fields defined in
:class:`BaseDecisionEvent` so every decision in the pipeline is fully
traceable from snapshot ingestion through to execution.

Usage::

    from contracts_app.decision_events import build_regime_decision_event

    event = build_regime_decision_event(
        trace_id=trace_id,
        parent_event_id=snapshot_event_id,
        run_id=run_id,
        parity_mode="live_full",
        plugin_id="regime_classifier_v1",
        plugin_version="1.0",
        regime="trend",
        confidence=0.84,
        snapshot_id=snapshot_id,
    )
    bus.publish(namespace.stream_for("regime_decisions"), event)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from uuid import uuid4

from .time_utils import isoformat_ist, now_ist


# ---------------------------------------------------------------------------
# Common envelope — required on every decision event
# ---------------------------------------------------------------------------


@dataclass
class BaseDecisionEvent:
    """Eight metadata fields required on every stage decision event."""

    event_id: str
    trace_id: str
    parent_event_id: str    # event_id of the upstream event that triggered this stage
    run_id: str
    timestamp: str          # ISO-8601 IST
    parity_mode: str        # ParityMode value string (e.g. "live_full")
    plugin_id: str
    plugin_version: str


# ---------------------------------------------------------------------------
# Stage 1 — Regime
# ---------------------------------------------------------------------------


@dataclass
class RegimeDecisionEvent(BaseDecisionEvent):
    event_type: str = "regime_decision"
    event_version: str = "1.0"
    regime: str = ""
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    snapshot_id: str = ""
    # Compact snapshot fields required by downstream entry/direction stages.
    # Avoids re-fetching from Redis for every downstream consumer.
    snapshot_summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 2 — Entry gate
# ---------------------------------------------------------------------------


@dataclass
class EntryDecisionEvent(BaseDecisionEvent):
    event_type: str = "entry_decision"
    event_version: str = "1.0"
    allowed: bool = False
    confidence: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    regime: str = ""
    snapshot_id: str = ""
    snapshot_summary: dict[str, Any] = field(default_factory=dict)
    # Serialised StrategyVote dicts needed by the direction stage
    strategy_votes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 3 — Direction (CE / PE)
# ---------------------------------------------------------------------------


@dataclass
class DirectionDecisionEvent(BaseDecisionEvent):
    event_type: str = "direction_decision"
    event_version: str = "1.0"
    vetoed: bool = False        # True when upstream entry gate rejected
    direction: str = ""         # "CE", "PE", or "" when vetoed
    confidence: float = 0.0
    reason: str = ""
    snapshot_id: str = ""
    strategy_votes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 4 — Depth quality check
# ---------------------------------------------------------------------------


@dataclass
class DepthDecisionEvent(BaseDecisionEvent):
    """Depth quality assessment — carries adjusted confidence for downstream stages.

    ``confidence`` is the upstream direction confidence possibly adjusted by depth:
    depth-aligned → higher confidence; depth-opposed → lower confidence.
    ``proceed=False`` only when DEPTH_HARD_GATE=1 and depth strongly disagrees.
    """
    event_type: str = "depth_decision"
    event_version: str = "1.0"
    proceed: bool = True
    confidence: float = 0.0          # adjusted direction confidence (from upstream + delta)
    skip_reason: Optional[str] = None
    direction: str = ""
    ce_bid_strength: Optional[float] = None   # CE bid qty / (bid+ask) qty, 0–1
    pe_bid_strength: Optional[float] = None   # PE bid qty / (bid+ask) qty, 0–1
    spread_pct: Optional[float] = None
    depth_aligned: bool = False              # depth direction matches trade direction
    depth_available: bool = False
    snapshot_id: str = ""
    snapshot_summary: dict[str, Any] = field(default_factory=dict)
    strategy_votes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 5 — Strike selection
# ---------------------------------------------------------------------------


@dataclass
class StrikeDecisionEvent(BaseDecisionEvent):
    event_type: str = "strike_decision"
    event_version: str = "1.0"
    skipped: bool = False       # True when upstream direction was vetoed
    strike: Optional[int] = None
    entry_premium: Optional[float] = None
    expiry: Optional[str] = None    # ISO date string
    position_side: str = ""         # "LONG" or "SHORT"
    direction: str = ""             # "CE" or "PE"
    snapshot_id: str = ""
    rationale: str = ""


# ---------------------------------------------------------------------------
# Stage 5 — Risk approval
# ---------------------------------------------------------------------------


@dataclass
class RiskDecisionEvent(BaseDecisionEvent):
    event_type: str = "risk_decision"
    event_version: str = "1.0"
    approved: bool = False
    approved_lots: int = 0
    rejection_reason: Optional[str] = None
    strike: Optional[int] = None
    entry_premium: Optional[float] = None
    expiry: Optional[str] = None
    position_side: str = ""
    direction: str = ""
    snapshot_id: str = ""


# ---------------------------------------------------------------------------
# Stage 6 — Execution
# ---------------------------------------------------------------------------


@dataclass
class ExecutionEvent(BaseDecisionEvent):
    event_type: str = "execution"
    event_version: str = "1.0"
    signal_type: str = "SKIP"   # "ENTER" or "SKIP"
    signal_id: str = ""
    direction: str = ""
    strike: Optional[int] = None
    entry_premium: Optional[float] = None
    expiry: Optional[str] = None
    position_side: str = ""
    lots: int = 0
    snapshot_id: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _base_kwargs(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "event_id": str(event_id or uuid4()),
        "trace_id": str(trace_id or ""),
        "parent_event_id": str(parent_event_id or ""),
        "run_id": str(run_id or ""),
        "timestamp": str(timestamp or isoformat_ist(now_ist())),
        "parity_mode": str(parity_mode or ""),
        "plugin_id": str(plugin_id or ""),
        "plugin_version": str(plugin_version or ""),
    }


def _to_dict(event: BaseDecisionEvent) -> dict[str, Any]:
    return asdict(event)


def _check_base(d: dict[str, Any], event_type: str) -> Optional[dict[str, Any]]:
    if str(d.get("event_type") or "") != event_type:
        return None
    if not d.get("event_id") or not d.get("trace_id"):
        return None
    return d


# ---------------------------------------------------------------------------
# Build / parse — Regime
# ---------------------------------------------------------------------------


def build_regime_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    regime: str,
    confidence: float,
    evidence: Optional[dict[str, Any]] = None,
    snapshot_id: str = "",
    snapshot_summary: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(RegimeDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        regime=str(regime or ""),
        confidence=float(confidence),
        evidence=dict(evidence or {}),
        snapshot_id=str(snapshot_id or ""),
        snapshot_summary=dict(snapshot_summary or {}),
    ))


def parse_regime_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "regime_decision")


# ---------------------------------------------------------------------------
# Build / parse — Entry
# ---------------------------------------------------------------------------


def build_entry_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    allowed: bool,
    confidence: float = 0.0,
    reason_codes: Optional[list[str]] = None,
    regime: str = "",
    snapshot_id: str = "",
    snapshot_summary: Optional[dict[str, Any]] = None,
    strategy_votes: Optional[list[dict[str, Any]]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(EntryDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        allowed=bool(allowed),
        confidence=float(confidence),
        reason_codes=list(reason_codes or []),
        regime=str(regime or ""),
        snapshot_id=str(snapshot_id or ""),
        snapshot_summary=dict(snapshot_summary or {}),
        strategy_votes=list(strategy_votes or []),
    ))


def parse_entry_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "entry_decision")


# ---------------------------------------------------------------------------
# Build / parse — Direction
# ---------------------------------------------------------------------------


def build_direction_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    vetoed: bool,
    direction: str = "",
    confidence: float = 0.0,
    reason: str = "",
    snapshot_id: str = "",
    strategy_votes: Optional[list[dict[str, Any]]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(DirectionDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        vetoed=bool(vetoed),
        direction=str(direction or ""),
        confidence=float(confidence),
        reason=str(reason or ""),
        snapshot_id=str(snapshot_id or ""),
        strategy_votes=list(strategy_votes or []),
    ))


def parse_direction_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "direction_decision")


# ---------------------------------------------------------------------------
# Build / parse — Depth
# ---------------------------------------------------------------------------


def build_depth_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    proceed: bool,
    confidence: float = 0.0,
    skip_reason: Optional[str] = None,
    direction: str = "",
    ce_bid_strength: Optional[float] = None,
    pe_bid_strength: Optional[float] = None,
    spread_pct: Optional[float] = None,
    depth_aligned: bool = False,
    depth_available: bool = False,
    snapshot_id: str = "",
    snapshot_summary: Optional[dict[str, Any]] = None,
    strategy_votes: Optional[list[dict[str, Any]]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(DepthDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        proceed=bool(proceed),
        confidence=float(confidence),
        skip_reason=str(skip_reason) if skip_reason is not None else None,
        direction=str(direction or ""),
        ce_bid_strength=float(ce_bid_strength) if ce_bid_strength is not None else None,
        pe_bid_strength=float(pe_bid_strength) if pe_bid_strength is not None else None,
        spread_pct=float(spread_pct) if spread_pct is not None else None,
        depth_aligned=bool(depth_aligned),
        depth_available=bool(depth_available),
        snapshot_id=str(snapshot_id or ""),
        snapshot_summary=dict(snapshot_summary or {}),
        strategy_votes=list(strategy_votes or []),
    ))


def parse_depth_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "depth_decision")


# ---------------------------------------------------------------------------
# Build / parse — Strike
# ---------------------------------------------------------------------------


def build_strike_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    skipped: bool,
    strike: Optional[int] = None,
    entry_premium: Optional[float] = None,
    expiry: Optional[str] = None,
    position_side: str = "",
    direction: str = "",
    snapshot_id: str = "",
    rationale: str = "",
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(StrikeDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        skipped=bool(skipped),
        strike=int(strike) if strike is not None else None,
        entry_premium=float(entry_premium) if entry_premium is not None else None,
        expiry=str(expiry) if expiry is not None else None,
        position_side=str(position_side or ""),
        direction=str(direction or ""),
        snapshot_id=str(snapshot_id or ""),
        rationale=str(rationale or ""),
    ))


def parse_strike_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "strike_decision")


# ---------------------------------------------------------------------------
# Build / parse — Risk
# ---------------------------------------------------------------------------


def build_risk_decision_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    approved: bool,
    approved_lots: int = 0,
    rejection_reason: Optional[str] = None,
    strike: Optional[int] = None,
    entry_premium: Optional[float] = None,
    expiry: Optional[str] = None,
    position_side: str = "",
    direction: str = "",
    snapshot_id: str = "",
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(RiskDecisionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        approved=bool(approved),
        approved_lots=int(approved_lots),
        rejection_reason=str(rejection_reason) if rejection_reason is not None else None,
        strike=int(strike) if strike is not None else None,
        entry_premium=float(entry_premium) if entry_premium is not None else None,
        expiry=str(expiry) if expiry is not None else None,
        position_side=str(position_side or ""),
        direction=str(direction or ""),
        snapshot_id=str(snapshot_id or ""),
    ))


def parse_risk_decision_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "risk_decision")


# ---------------------------------------------------------------------------
# Build / parse — Execution
# ---------------------------------------------------------------------------


def build_execution_event(
    *,
    trace_id: str,
    parent_event_id: str,
    run_id: str,
    parity_mode: str,
    plugin_id: str,
    plugin_version: str,
    signal_type: str,
    signal_id: str = "",
    direction: str = "",
    strike: Optional[int] = None,
    entry_premium: Optional[float] = None,
    expiry: Optional[str] = None,
    position_side: str = "",
    lots: int = 0,
    snapshot_id: str = "",
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _to_dict(ExecutionEvent(
        **_base_kwargs(
            trace_id=trace_id,
            parent_event_id=parent_event_id,
            run_id=run_id,
            parity_mode=parity_mode,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            event_id=event_id,
            timestamp=timestamp,
        ),
        signal_type=str(signal_type or "SKIP"),
        signal_id=str(signal_id or ""),
        direction=str(direction or ""),
        strike=int(strike) if strike is not None else None,
        entry_premium=float(entry_premium) if entry_premium is not None else None,
        expiry=str(expiry) if expiry is not None else None,
        position_side=str(position_side or ""),
        lots=int(lots),
        snapshot_id=str(snapshot_id or ""),
    ))


def parse_execution_event(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _check_base(dict(payload or {}), "execution")


__all__ = [
    "BaseDecisionEvent",
    "RegimeDecisionEvent",
    "EntryDecisionEvent",
    "DirectionDecisionEvent",
    "DepthDecisionEvent",
    "StrikeDecisionEvent",
    "RiskDecisionEvent",
    "ExecutionEvent",
    "build_regime_decision_event",
    "parse_regime_decision_event",
    "build_entry_decision_event",
    "parse_entry_decision_event",
    "build_direction_decision_event",
    "parse_direction_decision_event",
    "build_depth_decision_event",
    "parse_depth_decision_event",
    "build_strike_decision_event",
    "parse_strike_decision_event",
    "build_risk_decision_event",
    "parse_risk_decision_event",
    "build_execution_event",
    "parse_execution_event",
]
