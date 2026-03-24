from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from contracts_app.market_session import IST_ZONE

try:
    from ..strategy_monitor_contracts import AlertItem
except ImportError:
    from strategy_monitor_contracts import AlertItem  # type: ignore

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


def _coerce_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


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


def _nonnegative_threshold_from_env(env_key: str, default_value: float) -> float:
    raw = str(os.getenv(env_key, str(default_value))).strip()
    try:
        value = float(raw)
    except Exception:
        return float(default_value)
    if value != value or value in {float("inf"), float("-inf")}:
        return float(default_value)
    if value < 0.0:
        return float(default_value)
    return float(value)


def _rolling_threshold_meta(
    rolling_quality: dict[str, Any],
    *,
    threshold_key: str,
    section_key: str,
    section_threshold_key: str,
    env_key: str,
    default_value: float,
    nonnegative: bool = False,
) -> dict[str, Any]:
    thresholds = rolling_quality.get("thresholds") if isinstance(rolling_quality.get("thresholds"), dict) else {}
    persisted = thresholds.get(threshold_key) if isinstance(thresholds, dict) else {}
    if isinstance(persisted, dict):
        value = _safe_ratio(persisted.get("value"))
        if value is not None:
            if nonnegative and value < 0.0:
                value = None
            if value is not None:
                return {
                    "value": float(value),
                    "source": str(persisted.get("source") or "rolling_quality.thresholds"),
                    "mode": "persisted",
                }
    section = rolling_quality.get(section_key) if isinstance(rolling_quality.get(section_key), dict) else {}
    if isinstance(section, dict):
        value = _safe_ratio(section.get(section_threshold_key))
        if value is not None:
            if nonnegative and value < 0.0:
                value = None
            if value is not None:
                return {
                    "value": float(value),
                    "source": str(section.get(f"{section_threshold_key}_source") or f"rolling_quality.{section_key}"),
                    "mode": "persisted",
                }
    value = (
        _nonnegative_threshold_from_env(env_key, default_value)
        if nonnegative
        else _threshold_from_env(env_key, default_value)
    )
    return {
        "value": float(value),
        "source": f"env.{env_key}" if os.getenv(env_key) is not None else "default",
        "mode": "fallback",
    }


def _rolling_breach(
    rolling_quality: dict[str, Any],
    *,
    breach_key: str,
    metric_value: Optional[float],
    threshold_value: float,
    direction: str,
) -> bool:
    breaches = rolling_quality.get("breaches") if isinstance(rolling_quality.get("breaches"), dict) else {}
    persisted = _coerce_bool(breaches.get(breach_key)) if isinstance(breaches, dict) else None
    if persisted is not None:
        return persisted
    if metric_value is None:
        return False
    if direction == "lt":
        return float(metric_value) < float(threshold_value)
    if direction == "gt":
        return float(metric_value) > float(threshold_value)
    return False


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
    stale_position_count = len(stale_open_positions or [])
    exposure_positions = int(open_positions + stale_position_count)

    votes_fresh = bool(freshness.get("votes_fresh"))
    signals_fresh = bool(freshness.get("signals_fresh"))
    positions_fresh = bool(freshness.get("positions_fresh"))
    stale = not (votes_fresh and signals_fresh and positions_fresh)
    if stale and exposure_positions > 0:
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
    ml_pure_rolling = (ml_pure.get("rolling_quality") or {}) if isinstance(ml_pure, dict) else {}
    rolling_stage1 = (ml_pure_rolling.get("stage1_precision") or {}) if isinstance(ml_pure_rolling, dict) else {}
    rolling_pf = (ml_pure_rolling.get("profit_factor") or {}) if isinstance(ml_pure_rolling, dict) else {}
    rolling_regime = (ml_pure_rolling.get("regime_drift") or {}) if isinstance(ml_pure_rolling, dict) else {}
    rolling_status = str(ml_pure_rolling.get("status") or "").strip().lower()
    rolling_error = (ml_pure_rolling.get("error") or {}) if isinstance(ml_pure_rolling.get("error"), dict) else {}

    directional_votes = int(deterministic_counts.get("directional_entry_votes_day") or 0)
    warmup_blocked = int(deterministic_counts.get("warmup_blocked_votes_day") or 0)
    block_rate = _safe_ratio(deterministic_ratios.get("policy_block_rate_day"))
    hold_rate = _safe_ratio(ml_pure_ratios.get("hold_rate"))
    block_rate_warn_threshold = _threshold_from_env("LIVE_STRATEGY_ALERT_POLICY_BLOCK_RATE_WARN", 0.80)
    hold_rate_warn_threshold = _threshold_from_env("LIVE_STRATEGY_ALERT_ML_PURE_HOLD_RATE_WARN", 0.80)
    stage1_precision_warn_meta = _rolling_threshold_meta(
        ml_pure_rolling,
        threshold_key="stage1_precision_warning",
        section_key="stage1_precision",
        section_threshold_key="warning_threshold",
        env_key="LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN",
        default_value=0.50,
    )
    profit_factor_warn_meta = _rolling_threshold_meta(
        ml_pure_rolling,
        threshold_key="profit_factor_warning",
        section_key="profit_factor",
        section_threshold_key="warning_threshold",
        env_key="LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN",
        default_value=0.90,
        nonnegative=True,
    )
    regime_drift_info_meta = _rolling_threshold_meta(
        ml_pure_rolling,
        threshold_key="regime_drift_info",
        section_key="regime_drift",
        section_threshold_key="info_threshold",
        env_key="LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO",
        default_value=0.20,
    )

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
    if rolling_status == "error":
        error_type = str(rolling_error.get("type") or "MonitoringError").strip() or "MonitoringError"
        error_message = str(rolling_error.get("message") or "").strip()
        detail = f"Rolling ML quality evaluation failed: {error_type}"
        if error_message:
            detail = f"{detail} ({error_message})"
        _add_alert(
            alerts,
            alert_id="ml_pure_monitoring_failure",
            severity="warning",
            title="ML-Pure Monitoring Failure",
            detail=detail,
            source="decision_diagnostics.ml_pure.rolling_quality",
            operator_next_step="Inspect evaluation-service logs and restore rolling-quality inputs before trusting monitoring.",
        )
    elif rolling_status == "unavailable":
        reason = str(ml_pure_rolling.get("reason") or "").strip()
        detail = "Rolling ML quality evaluation is unavailable."
        if reason:
            detail = f"{detail} Reason: {reason}."
        _add_alert(
            alerts,
            alert_id="ml_pure_monitoring_unavailable",
            severity="warning",
            title="ML-Pure Monitoring Unavailable",
            detail=detail,
            source="decision_diagnostics.ml_pure.rolling_quality",
            operator_next_step="Provide the positions collection and rolling-quality evaluator inputs before relying on monitoring.",
        )
    rolling_stage1_precision = _safe_ratio(rolling_stage1.get("precision"))
    if _rolling_breach(
        ml_pure_rolling,
        breach_key="stage1_precision_warning",
        metric_value=rolling_stage1_precision,
        threshold_value=float(stage1_precision_warn_meta["value"]),
        direction="lt",
    ):
        precision_display = (
            f"{rolling_stage1_precision * 100.0:.1f}%"
            if rolling_stage1_precision is not None
            else "unknown"
        )
        _add_alert(
            alerts,
            alert_id="ml_pure_stage1_precision_degraded",
            severity="warning",
            title="ML-Pure Stage 1 Precision Degraded",
            detail=(
                f"Rolling Stage 1 precision is {precision_display} < "
                f"{float(stage1_precision_warn_meta['value']) * 100.0:.1f}% "
                f"({stage1_precision_warn_meta['source']})."
            ),
            source="decision_diagnostics.ml_pure.rolling_quality",
            operator_next_step="Review deployed Stage 1 threshold and recent entry-quality drift.",
        )
    rolling_profit_factor = _safe_ratio(rolling_pf.get("profit_factor"))
    if _rolling_breach(
        ml_pure_rolling,
        breach_key="profit_factor_warning",
        metric_value=rolling_profit_factor,
        threshold_value=float(profit_factor_warn_meta["value"]),
        direction="lt",
    ):
        profit_factor_display = f"{rolling_profit_factor:.2f}" if rolling_profit_factor is not None else "unknown"
        _add_alert(
            alerts,
            alert_id="ml_pure_profit_factor_degraded",
            severity="warning",
            title="ML-Pure Rolling Profit Factor Degraded",
            detail=(
                f"Rolling profit factor is {profit_factor_display} < "
                f"{float(profit_factor_warn_meta['value']):.2f} "
                f"({profit_factor_warn_meta['source']})."
            ),
            source="decision_diagnostics.ml_pure.rolling_quality",
            operator_next_step="Inspect recent losses by regime and compare against training baseline.",
        )
    max_regime_shift = _safe_ratio(rolling_regime.get("max_abs_shift"))
    if _rolling_breach(
        ml_pure_rolling,
        breach_key="regime_drift_info",
        metric_value=max_regime_shift,
        threshold_value=float(regime_drift_info_meta["value"]),
        direction="gt",
    ):
        regime_shift_display = f"{max_regime_shift * 100.0:.1f}%" if max_regime_shift is not None else "unknown"
        _add_alert(
            alerts,
            alert_id="ml_pure_regime_drift",
            severity="info",
            title="ML-Pure Regime Mix Drift",
            detail=(
                f"Rolling regime-share drift reached {regime_shift_display} > "
                f"{float(regime_drift_info_meta['value']) * 100.0:.1f}% "
                f"({regime_drift_info_meta['source']})."
            ),
            source="decision_diagnostics.ml_pure.rolling_quality",
            operator_next_step="Compare live regime mix against training summary before changing thresholds.",
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

    if stale_position_count > 0:
        _add_alert(
            alerts,
            alert_id="stale_open_positions_detected",
            severity="warning",
            title="Stale Open Position Records",
            detail=f"{stale_position_count} open position record(s) are lagging latest state.",
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
