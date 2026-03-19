from __future__ import annotations

from typing import Any, Optional, TypedDict


class EngineContext(TypedDict, total=False):
    active_engine_mode: Optional[str]
    latest_engine_mode: Optional[str]
    strategy_family_version: Optional[str]
    strategy_profile_id: Optional[str]
    observed_engine_modes: dict[str, int]


class DeterministicDiagnostics(TypedDict, total=False):
    status: str
    summary: str
    counts: dict[str, Any]
    ratios: dict[str, Any]
    latest_policy_decision: Optional[dict[str, Any]]
    recent_policy_decisions: list[dict[str, Any]]


class MlPureDiagnostics(TypedDict, total=False):
    status: str
    summary: str
    counts: dict[str, Any]
    ratios: dict[str, Any]
    hold_reasons: dict[str, int]
    edge_distribution: dict[str, Any]
    confidence_distribution: dict[str, Any]
    latest_decision: Optional[dict[str, Any]]
    recent_decisions: list[dict[str, Any]]


class DecisionDiagnostics(TypedDict, total=False):
    deterministic: DeterministicDiagnostics
    ml_pure: MlPureDiagnostics


class OpsState(TypedDict, total=False):
    market_state: str
    engine_state: str
    risk_state: str
    data_health_state: str
    active_blocker: Optional[str]


class AlertItem(TypedDict, total=False):
    id: str
    severity: str
    title: str
    detail: str
    first_seen_ist: str
    last_seen_ist: str
    occurrences: int
    source: str
    operator_next_step: str


class DecisionTimelineItem(TypedDict, total=False):
    id: str
    ts: Optional[str]
    engine_mode: Optional[str]
    decision_mode: Optional[str]
    action: str
    reason_code: str
    explanation: str
    operator_hint: str
    metrics: dict[str, Any]
    source_ref: str
    gate_path: str


class DecisionExplainability(TypedDict, total=False):
    latest_decision: Optional[DecisionTimelineItem]
    timeline: list[DecisionTimelineItem]
    gate_funnel: dict[str, Any]
    reason_playbook_summary: list[dict[str, Any]]


class UiHints(TypedDict, total=False):
    active_engine_panel: str
    recommended_focus_panel: str
    degraded_mode: bool
    debug_view: bool


class LiveStrategySessionPayload(TypedDict, total=False):
    status: str
    session: dict[str, Any]
    engine_context: EngineContext
    promotion_lane: str
    capital: dict[str, Any]
    counts: dict[str, Any]
    warnings: list[str]
    current_position: Optional[dict[str, Any]]
    current_positions: list[dict[str, Any]]
    stale_open_positions: list[dict[str, Any]]
    reconciliation: dict[str, Any]
    latest_closed_trade: Optional[dict[str, Any]]
    session_chart: Optional[dict[str, Any]]
    today_summary: dict[str, Any]
    recent_trades: list[dict[str, Any]]
    recent_activity: list[dict[str, Any]]
    recent_signals: list[dict[str, Any]]
    recent_votes: list[dict[str, Any]]
    decision_diagnostics: DecisionDiagnostics
    ops_state: OpsState
    active_alerts: list[AlertItem]
    decision_explainability: DecisionExplainability
    ui_hints: UiHints
    chart_markers: list[dict[str, Any]]


__all__ = [
    "EngineContext",
    "DeterministicDiagnostics",
    "MlPureDiagnostics",
    "DecisionDiagnostics",
    "OpsState",
    "AlertItem",
    "DecisionTimelineItem",
    "DecisionExplainability",
    "UiHints",
    "LiveStrategySessionPayload",
]
