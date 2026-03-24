from __future__ import annotations

from typing import Any, Optional

from contracts_app.strategy_decision_contract import normalize_engine_mode

try:
    from .strategy_monitor_contracts import (
        AlertItem,
        DecisionExplainability,
        EngineContext,
        LiveStrategySessionPayload,
        OpsState,
        UiHints,
    )
except ImportError:
    from strategy_monitor_contracts import (  # type: ignore
        AlertItem,
        DecisionExplainability,
        EngineContext,
        LiveStrategySessionPayload,
        OpsState,
        UiHints,
    )


def infer_engine_context(
    *,
    recent_votes: list[dict[str, Any]],
    recent_signals: list[dict[str, Any]],
) -> EngineContext:
    observed: dict[str, int] = {}
    latest_engine_mode: Optional[str] = None
    latest_family: Optional[str] = None
    latest_profile: Optional[str] = None

    for row in list(recent_signals) + list(recent_votes):
        mode = normalize_engine_mode(row.get("engine_mode"))
        if mode is None:
            continue
        observed[mode] = int(observed.get(mode, 0) + 1)
        if latest_engine_mode is None:
            latest_engine_mode = mode
            latest_family = str(row.get("strategy_family_version") or "").strip() or None
            latest_profile = str(row.get("strategy_profile_id") or "").strip() or None

    active_engine_mode = latest_engine_mode
    if active_engine_mode is None and observed:
        active_engine_mode = max(observed.items(), key=lambda item: item[1])[0]

    return {
        "active_engine_mode": active_engine_mode,
        "latest_engine_mode": latest_engine_mode,
        "strategy_family_version": latest_family,
        "strategy_profile_id": latest_profile,
        "observed_engine_modes": observed,
    }


def promotion_lane_from_engine(active_engine_mode: Optional[str]) -> str:
    if str(active_engine_mode or "").strip().lower() == "ml_pure":
        return "ml_pure"
    return "deterministic"


def build_session_payload(
    *,
    session: dict[str, Any],
    engine_context: EngineContext,
    promotion_lane: str,
    capital: dict[str, Any],
    counts: dict[str, Any],
    warnings: list[str],
    current_position: Optional[dict[str, Any]],
    current_positions: list[dict[str, Any]],
    stale_open_positions: list[dict[str, Any]],
    reconciliation: dict[str, Any],
    latest_closed_trade: Optional[dict[str, Any]],
    session_chart: Optional[dict[str, Any]],
    today_summary: dict[str, Any],
    recent_trades: list[dict[str, Any]],
    recent_activity: list[dict[str, Any]],
    recent_signals: list[dict[str, Any]],
    recent_votes: list[dict[str, Any]],
    decision_diagnostics: dict[str, Any],
    ops_state: Optional[OpsState] = None,
    active_alerts: Optional[list[AlertItem]] = None,
    decision_explainability: Optional[DecisionExplainability] = None,
    ui_hints: Optional[UiHints] = None,
    chart_markers: list[dict[str, Any]],
) -> LiveStrategySessionPayload:
    payload: LiveStrategySessionPayload = {
        "status": "ok",
        "session": session,
        "engine_context": engine_context,
        "promotion_lane": promotion_lane,
        "capital": capital,
        "counts": counts,
        "warnings": warnings,
        "current_position": current_position,
        "current_positions": current_positions,
        "stale_open_positions": stale_open_positions,
        "reconciliation": reconciliation,
        "latest_closed_trade": latest_closed_trade,
        "session_chart": session_chart,
        "today_summary": today_summary,
        "recent_trades": recent_trades,
        "recent_activity": recent_activity,
        "recent_signals": recent_signals,
        "recent_votes": recent_votes,
        "decision_diagnostics": decision_diagnostics,
        "chart_markers": chart_markers,
    }
    if ops_state is not None:
        payload["ops_state"] = ops_state
    if active_alerts is not None:
        payload["active_alerts"] = active_alerts
    if decision_explainability is not None:
        payload["decision_explainability"] = decision_explainability
    if ui_hints is not None:
        payload["ui_hints"] = ui_hints
    return payload


__all__ = [
    "infer_engine_context",
    "promotion_lane_from_engine",
    "build_session_payload",
]
