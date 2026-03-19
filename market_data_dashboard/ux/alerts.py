from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from contracts_app.market_session import IST_ZONE

from ..strategy_monitor_contracts import AlertItem

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _now_ist_iso() -> str:
    return datetime.now(tz=IST_ZONE).isoformat()


def _safe_ratio(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if out != out or out in {float("inf"), float("-inf")}:
        return None
    return float(out)


def _threshold_from_env(env_key: str, default_value: float) -> float:
    raw = str(os.getenv(env_key, str(default_value))).strip()
    try:
        value = float(raw)
    except Exception:
        return float(default_value)
    if value != value or value in {float("inf"), float("-inf")}:
        return float(default_value)
    if value < 0.0 or value > 1.0:
        return float(default_value)
    return float(value)


def _add_alert(
    store: dict[str, AlertItem],
    *,
    alert_id: str,
    severity: str,
    title: str,
    detail: str,
    source: str,
    operator_next_step: str,
) -> None:
    now = _now_ist_iso()
    if alert_id in store:
        item = store[alert_id]
        item["occurrences"] = int(item.get("occurrences") or 0) + 1
        item["last_seen_ist"] = now
        # Escalate severity only upward.
        existing = str(item.get("severity") or "info")
        if _SEVERITY_RANK.get(str(severity), 2) < _SEVERITY_RANK.get(existing, 2):
            item["severity"] = severity
        return
    store[alert_id] = {
        "id": alert_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "first_seen_ist": now,
        "last_seen_ist": now,
        "occurrences": 1,
        "source": source,
        "operator_next_step": operator_next_step,
    }


def build_active_alerts(
    *,
    freshness: dict[str, Any],
    stale_open_positions: list[dict[str, Any]],
    warnings: list[str],
    engine_context: dict[str, Any],
    decision_diagnostics: dict[str, Any],
    counts: dict[str, Any],
    latest_decision: Optional[dict[str, Any]] = None,
    previous_engine_mode: Optional[str] = None,
) -> list[AlertItem]:
    alerts: dict[str, AlertItem] = {}
    open_positions = int(counts.get("open_positions") or 0)

    votes_fresh = bool(freshness.get("votes_fresh"))
    signals_fresh = bool(freshness.get("signals_fresh"))
    positions_fresh = bool(freshness.get("positions_fresh"))
    stale = not (votes_fresh and signals_fresh and positions_fresh)
    if stale and open_positions > 0:
        _add_alert(
            alerts,
            alert_id="data_stale_with_exposure",
            severity="critical",
            title="Data Freshness Degraded With Open Exposure",
            detail="One or more live streams are stale while open positions exist.",
            source="freshness",
            operator_next_step="Validate snapshot/signal ingestion latency immediately.",
        )
    elif stale:
        _add_alert(
            alerts,
            alert_id="data_stale",
            severity="warning",
            title="Data Freshness Degraded",
            detail="At least one live data stream is stale.",
            source="freshness",
            operator_next_step="Check stream health before trusting new entry signals.",
        )

    deterministic = (decision_diagnostics.get("deterministic") or {}) if isinstance(decision_diagnostics, dict) else {}
    ml_pure = (decision_diagnostics.get("ml_pure") or {}) if isinstance(decision_diagnostics, dict) else {}
    deterministic_counts = (deterministic.get("counts") or {}) if isinstance(deterministic, dict) else {}
    deterministic_ratios = (deterministic.get("ratios") or {}) if isinstance(deterministic, dict) else {}
    ml_pure_ratios = (ml_pure.get("ratios") or {}) if isinstance(ml_pure, dict) else {}

    directional_votes = int(deterministic_counts.get("directional_entry_votes_day") or 0)
    warmup_blocked = int(deterministic_counts.get("warmup_blocked_votes_day") or 0)
    block_rate = _safe_ratio(deterministic_ratios.get("policy_block_rate_day"))
    hold_rate = _safe_ratio(ml_pure_ratios.get("hold_rate"))
    block_rate_warn_threshold = _threshold_from_env("LIVE_STRATEGY_ALERT_POLICY_BLOCK_RATE_WARN", 0.80)
    hold_rate_warn_threshold = _threshold_from_env("LIVE_STRATEGY_ALERT_ML_PURE_HOLD_RATE_WARN", 0.80)

    latest_reason = str((latest_decision or {}).get("reason_code") or "").strip().lower()
    if latest_reason == "risk_halt" and directional_votes > 0:
        _add_alert(
            alerts,
            alert_id="risk_halt_with_candidates",
            severity="critical",
            title="Risk Halt Active With Entry Candidates",
            detail="Risk halt is blocking execution while directional opportunities are present.",
            source="risk",
            operator_next_step="Inspect risk limits/drawdown state before resuming.",
        )
    elif latest_reason == "risk_pause":
        _add_alert(
            alerts,
            alert_id="risk_pause_active",
            severity="warning",
            title="Risk Pause Active",
            detail="Risk pause is currently preventing new entries.",
            source="risk",
            operator_next_step="Review pause trigger and resume criteria.",
        )

    if block_rate is not None and block_rate >= block_rate_warn_threshold:
        _add_alert(
            alerts,
            alert_id="high_policy_block_rate",
            severity="warning",
            title="High Deterministic Policy Block Rate",
            detail=f"Deterministic policy block rate is elevated ({block_rate * 100.0:.1f}% >= {block_rate_warn_threshold * 100.0:.1f}%).",
            source="decision_diagnostics.deterministic",
            operator_next_step="Review deterministic policy checks and base candidate quality.",
        )
    if hold_rate is not None and hold_rate >= hold_rate_warn_threshold:
        _add_alert(
            alerts,
            alert_id="high_ml_pure_hold_rate",
            severity="warning",
            title="High ML-Pure HOLD Rate",
            detail=f"ML-pure hold rate is elevated ({hold_rate * 100.0:.1f}% >= {hold_rate_warn_threshold * 100.0:.1f}%).",
            source="decision_diagnostics.ml_pure",
            operator_next_step="Inspect low-edge and feature-quality hold reasons.",
        )

    if str(engine_context.get("active_engine_mode") or "").strip() == "":
        _add_alert(
            alerts,
            alert_id="engine_context_missing",
            severity="warning",
            title="Engine Context Missing",
            detail="Active engine mode could not be inferred from recent events.",
            source="engine_context",
            operator_next_step="Verify lane metadata persistence on vote/signal events.",
        )

    stale_count = len(stale_open_positions or [])
    if stale_count > 0:
        _add_alert(
            alerts,
            alert_id="stale_open_positions_detected",
            severity="warning",
            title="Stale Open Position Records",
            detail=f"{stale_count} open position record(s) are lagging latest state.",
            source="reconciliation",
            operator_next_step="Cross-check broker/position tracker state before acting.",
        )

    current_engine_mode = str(engine_context.get("active_engine_mode") or "").strip()
    if previous_engine_mode and current_engine_mode and previous_engine_mode != current_engine_mode:
        _add_alert(
            alerts,
            alert_id="engine_lane_switched",
            severity="info",
            title="Engine Lane Switched",
            detail=f"Active lane changed from {previous_engine_mode} to {current_engine_mode}.",
            source="engine_context",
            operator_next_step="Confirm diagnostics panel focus matches active lane.",
        )

    if warmup_blocked > 0:
        _add_alert(
            alerts,
            alert_id="warmup_blocks_present",
            severity="info",
            title="Warmup Blocks Observed",
            detail=f"Warmup blocked {warmup_blocked} candidate(s) today.",
            source="decision_diagnostics.deterministic",
            operator_next_step="No action unless warmup extends beyond expected window.",
        )

    if directional_votes <= 0:
        _add_alert(
            alerts,
            alert_id="no_directional_votes_today",
            severity="info",
            title="No Directional Votes Today",
            detail="No CE/PE directional entry votes recorded in current session window.",
            source="decision_diagnostics.deterministic",
            operator_next_step="Check whether session phase or regime is intentionally restrictive.",
        )

    for warning in warnings or []:
        token = str(warning or "").strip().lower()
        if not token:
            continue
        _add_alert(
            alerts,
            alert_id=f"warning_{token}",
            severity="warning",
            title=token.replace("_", " ").title(),
            detail=f"Service warning emitted: {token}",
            source="service.warnings",
            operator_next_step="Inspect corresponding diagnostics section for details.",
        )

    rows = list(alerts.values())
    rows.sort(key=lambda row: (_SEVERITY_RANK.get(str(row.get("severity") or "info"), 2), -int(row.get("occurrences") or 0), str(row.get("id") or "")))
    return rows


__all__ = [
    "build_active_alerts",
]
