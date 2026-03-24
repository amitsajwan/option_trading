from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from contracts_app.strategy_decision_contract import normalize_decision_mode, normalize_engine_mode, normalize_reason_code

try:
    from ..strategy_monitor_contracts import DecisionExplainability, DecisionTimelineItem
except ImportError:
    from strategy_monitor_contracts import DecisionExplainability, DecisionTimelineItem  # type: ignore

_REASON_PLAYBOOK: dict[str, tuple[str, str]] = {
    "below_threshold": (
        "Model confidence did not clear the configured decision threshold.",
        "Wait for stronger setup or review threshold policy if this persists.",
    ),
    "low_edge_conflict": (
        "CE and PE probabilities are too close; edge is insufficient.",
        "Avoid forcing direction; monitor edge distribution and hold rate.",
    ),
    "feature_stale": (
        "Feature snapshot is too old for safe decisioning.",
        "Check data pipeline latency and snapshot freshness.",
    ),
    "feature_incomplete": (
        "Required model features were missing or invalid.",
        "Verify feature completeness and source stream health.",
    ),
    "sideways_block": (
        "Regime filter marked market as sideways and blocked entry.",
        "Stand by; wait for regime transition confirmation.",
    ),
    "avoid_regime": (
        "Regime classifier marked this period as avoid/no-trade.",
        "Do not override; monitor for regime normalization.",
    ),
    "regime_low_confidence": (
        "Regime confidence is below safe execution threshold.",
        "Wait for clearer regime signal.",
    ),
    "below_min_confidence": (
        "Final confidence is below minimum runtime requirement.",
        "No action needed; monitor repeated occurrences.",
    ),
    "entry_warmup_block": (
        "Startup warmup gate blocked new entries.",
        "Wait until warmup window closes.",
    ),
    "policy_block": (
        "Deterministic policy checks failed for this candidate.",
        "Review policy checks in diagnostics for the failing condition.",
    ),
    "policy_allowed": (
        "Policy checks passed and candidate was allowed.",
        "If expected entry did not happen, inspect downstream risk/phase gates.",
    ),
    "time_stop": (
        "Position exited due to configured time stop.",
        "Review hold duration settings if exits are too early/late.",
    ),
    "stop_loss": (
        "Position exited via stop loss.",
        "Check risk sizing and stop placement consistency.",
    ),
    "target_hit": (
        "Position exited after reaching target.",
        "No action needed; validate reward/risk continuity.",
    ),
    "risk_halt": (
        "Risk layer halted entry execution.",
        "Inspect risk controls and session drawdown state immediately.",
    ),
    "risk_pause": (
        "Risk layer paused entry execution.",
        "Monitor pause condition and resume criteria.",
    ),
}


def _parse_iso_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if out != out or out in {float("inf"), float("-inf")}:
        return None
    return float(out)


def explain_reason_code(reason_code: Any) -> tuple[str, str]:
    code = normalize_reason_code(reason_code) or "unknown"
    if code in _REASON_PLAYBOOK:
        return _REASON_PLAYBOOK[code]
    return (
        "Decision reason is available but not mapped to an operator playbook entry.",
        "Use diagnostics details to inspect the exact gate path.",
    )


def _gate_path(decision_mode: Optional[str], action: str) -> str:
    mode = normalize_decision_mode(decision_mode) or "rule_vote"
    if mode == "ml_staged":
        return f"Candidate -> ML Staged -> Risk/Phase -> {action}"
    return f"Candidate -> Policy -> Risk/Phase -> {action}"


def _signal_timeline_item(row: dict[str, Any]) -> DecisionTimelineItem:
    action = str(row.get("signal_type") or "HOLD").strip().upper() or "HOLD"
    code = normalize_reason_code(row.get("decision_reason_code")) or "unknown"
    explanation, operator_hint = explain_reason_code(code)
    metrics = row.get("decision_metrics") if isinstance(row.get("decision_metrics"), dict) else {}
    return {
        "id": str(row.get("signal_id") or f"signal:{row.get('timestamp')}:{row.get('strategy')}" or ""),
        "ts": row.get("timestamp"),
        "engine_mode": normalize_engine_mode(row.get("engine_mode")),
        "decision_mode": normalize_decision_mode(row.get("decision_mode")),
        "action": action,
        "reason_code": code,
        "explanation": explanation,
        "operator_hint": operator_hint,
        "metrics": metrics,
        "source_ref": f"signal:{str(row.get('signal_id') or '').strip() or 'unknown'}",
        "gate_path": _gate_path(row.get("decision_mode"), action),
    }


def _vote_timeline_item(row: dict[str, Any]) -> DecisionTimelineItem:
    signal_type = str(row.get("signal_type") or "").strip().upper()
    action = signal_type or ("ENTRY" if row.get("policy_allowed") is True else "HOLD")
    code = normalize_reason_code(row.get("decision_reason_code"))
    if code is None:
        code = "policy_allowed" if row.get("policy_allowed") is True else "policy_block"
    explanation, operator_hint = explain_reason_code(code)
    metrics = row.get("decision_metrics") if isinstance(row.get("decision_metrics"), dict) else {}
    if "confidence" not in metrics and _safe_float(row.get("confidence")) is not None:
        metrics = dict(metrics)
        metrics["confidence"] = float(_safe_float(row.get("confidence")) or 0.0)
    snapshot_id = str(row.get("snapshot_id") or "").strip() or "unknown"
    strategy = str(row.get("strategy") or "").strip() or "unknown"
    return {
        "id": f"vote:{snapshot_id}:{strategy}:{str(row.get('timestamp') or '')}",
        "ts": row.get("timestamp"),
        "engine_mode": normalize_engine_mode(row.get("engine_mode")),
        "decision_mode": normalize_decision_mode(row.get("decision_mode")),
        "action": action,
        "reason_code": code,
        "explanation": explanation,
        "operator_hint": operator_hint,
        "metrics": metrics,
        "source_ref": f"vote:{snapshot_id}:{strategy}",
        "gate_path": _gate_path(row.get("decision_mode"), action),
    }


def _sort_timeline(rows: list[DecisionTimelineItem]) -> list[DecisionTimelineItem]:
    return sorted(
        rows,
        key=lambda item: (_parse_iso_ts(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )


def _reason_summary(rows: list[DecisionTimelineItem]) -> list[dict[str, Any]]:
    counts = Counter(str(row.get("reason_code") or "unknown") for row in rows)
    out: list[dict[str, Any]] = []
    for code, count in counts.most_common(8):
        explanation, operator_hint = explain_reason_code(code)
        out.append(
            {
                "reason_code": code,
                "count": int(count),
                "explanation": explanation,
                "operator_hint": operator_hint,
            }
        )
    return out


def _gate_funnel(decision_diagnostics: dict[str, Any]) -> dict[str, Any]:
    deterministic = (decision_diagnostics.get("deterministic") or {}) if isinstance(decision_diagnostics, dict) else {}
    ml_pure = (decision_diagnostics.get("ml_pure") or {}) if isinstance(decision_diagnostics, dict) else {}
    deterministic_counts = (deterministic.get("counts") or {}) if isinstance(deterministic, dict) else {}
    ml_pure_counts = (ml_pure.get("counts") or {}) if isinstance(ml_pure, dict) else {}
    return {
        "directional_entry_votes_day": int(deterministic_counts.get("directional_entry_votes_day") or 0),
        "policy_evaluated_votes_day": int(deterministic_counts.get("policy_evaluated_votes_day") or 0),
        "policy_allowed_votes_day": int(deterministic_counts.get("policy_allowed_votes_day") or 0),
        "policy_blocked_votes_day": int(deterministic_counts.get("policy_blocked_votes_day") or 0),
        "ml_pure_entries_ce": int(ml_pure_counts.get("entries_ce") or 0),
        "ml_pure_entries_pe": int(ml_pure_counts.get("entries_pe") or 0),
        "ml_pure_holds": int(ml_pure_counts.get("holds") or 0),
    }


def build_decision_explainability(
    *,
    recent_signals: list[dict[str, Any]],
    recent_votes: list[dict[str, Any]],
    decision_diagnostics: dict[str, Any],
    timeline_limit: int = 25,
    debug_view: bool = False,
) -> DecisionExplainability:
    limit = max(1, min(100, int(timeline_limit)))
    timeline: list[DecisionTimelineItem] = []
    for row in recent_signals:
        if not isinstance(row, dict):
            continue
        timeline.append(_signal_timeline_item(row))

    if not timeline or bool(debug_view):
        for row in recent_votes:
            if not isinstance(row, dict):
                continue
            timeline.append(_vote_timeline_item(row))

    timeline = _sort_timeline(timeline)[:limit]
    return {
        "latest_decision": (timeline[0] if timeline else None),
        "timeline": timeline,
        "gate_funnel": _gate_funnel(decision_diagnostics),
        "reason_playbook_summary": _reason_summary(timeline),
    }


__all__ = [
    "build_decision_explainability",
    "explain_reason_code",
]
