from __future__ import annotations

import os
from typing import Any, Optional

from contracts_app.strategy_decision_contract import (
    normalize_decision_mode,
    normalize_engine_mode,
)

try:
    from persistence_app.strategy_evaluation import rolling_ml_quality_from_collections as _rolling_ml_quality_from_collections
except Exception:  # pragma: no cover
    _rolling_ml_quality_from_collections = None

try:
    from ..strategy_evaluation_service import _iso_or_none, _safe_float
except ImportError:
    from strategy_evaluation_service import _iso_or_none, _safe_float  # type: ignore


def _safe_ratio(numerator: Any, denominator: Any) -> Optional[float]:
    try:
        num = float(numerator)
        den = float(denominator)
    except Exception:
        return None
    if den <= 0:
        return None
    return num / den


def _distribution(values: list[float]) -> dict[str, Any]:
    arr = [float(v) for v in values if _safe_float(v) is not None]
    if not arr:
        return {"samples": 0, "min": None, "p50": None, "p90": None, "max": None, "mean": None}
    arr.sort()
    p50_idx = int(round(0.50 * (len(arr) - 1)))
    p90_idx = int(round(0.90 * (len(arr) - 1)))
    return {
        "samples": int(len(arr)),
        "min": float(arr[0]),
        "p50": float(arr[p50_idx]),
        "p90": float(arr[p90_idx]),
        "max": float(arr[-1]),
        "mean": float(sum(arr) / len(arr)),
    }


def build_ml_pure_diagnostics(*, date_ist: str, signals_coll: Any, positions_coll: Any = None) -> dict[str, Any]:
    lookback_raw = str(os.getenv("LIVE_STRATEGY_ML_PURE_DIAG_LOOKBACK") or "400").strip()
    try:
        lookback = int(lookback_raw)
    except Exception:
        lookback = 400
    lookback = max(50, min(2000, lookback))

    query = {"trade_date_ist": str(date_ist)}
    projection = {
        "_id": 0,
        "signal_id": 1,
        "timestamp": 1,
        "signal_type": 1,
        "direction": 1,
        "source": 1,
        "reason": 1,
        "confidence": 1,
        "engine_mode": 1,
        "decision_mode": 1,
        "decision_reason_code": 1,
        "decision_metrics": 1,
        "ml_entry_prob": 1,
        "ml_direction_up_prob": 1,
        "ml_ce_prob": 1,
        "ml_pe_prob": 1,
        "ml_recipe_prob": 1,
        "ml_recipe_margin": 1,
        "strategy_family_version": 1,
        "strategy_profile_id": 1,
        "payload.signal": 1,
    }
    docs = list(signals_coll.find(query, projection).sort("timestamp", -1).limit(int(lookback)))
    rows: list[dict[str, Any]] = []
    for doc in docs:
        signal = ((doc.get("payload") or {}).get("signal")) if isinstance(doc.get("payload"), dict) else {}
        signal = signal if isinstance(signal, dict) else {}
        engine_mode = normalize_engine_mode(doc.get("engine_mode") or signal.get("engine_mode"))
        if engine_mode is None:
            source = str(doc.get("source") or signal.get("source") or "").strip().upper()
            if source == "ML_PURE":
                engine_mode = "ml_pure"
        if engine_mode != "ml_pure":
            continue
        decision_metrics = doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else {}
        if not decision_metrics and isinstance(signal.get("decision_metrics"), dict):
            decision_metrics = dict(signal.get("decision_metrics") or {})
        direction_up = _safe_float(
            doc.get("ml_direction_up_prob")
            if doc.get("ml_direction_up_prob") is not None
            else decision_metrics.get("direction_up_prob")
        )
        ce_prob = _safe_float(doc.get("ml_ce_prob") if doc.get("ml_ce_prob") is not None else decision_metrics.get("ce_prob"))
        pe_prob = _safe_float(doc.get("ml_pe_prob") if doc.get("ml_pe_prob") is not None else decision_metrics.get("pe_prob"))
        if ce_prob is None and direction_up is not None:
            ce_prob = float(direction_up)
        if pe_prob is None and direction_up is not None:
            pe_prob = float(1.0 - direction_up)
        row = {
            "signal_id": str(doc.get("signal_id") or signal.get("signal_id") or "").strip() or None,
            "timestamp": _iso_or_none(doc.get("timestamp") or signal.get("timestamp")),
            "signal_type": str(doc.get("signal_type") or signal.get("signal_type") or "").strip().upper() or None,
            "direction": str(doc.get("direction") or signal.get("direction") or "").strip().upper() or None,
            "reason": str(doc.get("reason") or signal.get("reason") or "").strip() or None,
            "decision_reason_code": str(
                doc.get("decision_reason_code") or signal.get("decision_reason_code") or ""
            ).strip()
            or None,
            "decision_mode": normalize_decision_mode(doc.get("decision_mode") or signal.get("decision_mode")),
            "engine_mode": engine_mode,
            "confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else signal.get("confidence")),
            "decision_metrics": decision_metrics,
            "ml_entry_prob": _safe_float(doc.get("ml_entry_prob") if doc.get("ml_entry_prob") is not None else decision_metrics.get("entry_prob")),
            "ml_direction_up_prob": direction_up,
            "ml_ce_prob": ce_prob,
            "ml_pe_prob": pe_prob,
            "ml_recipe_prob": _safe_float(doc.get("ml_recipe_prob") if doc.get("ml_recipe_prob") is not None else decision_metrics.get("recipe_prob")),
            "ml_recipe_margin": _safe_float(doc.get("ml_recipe_margin") if doc.get("ml_recipe_margin") is not None else decision_metrics.get("recipe_margin")),
            "strategy_family_version": str(
                doc.get("strategy_family_version") or signal.get("strategy_family_version") or ""
            ).strip()
            or None,
            "strategy_profile_id": str(
                doc.get("strategy_profile_id") or signal.get("strategy_profile_id") or ""
            ).strip()
            or None,
        }
        if row["decision_reason_code"] is None:
            reason_lower = str(row.get("reason") or "").strip().lower()
            if reason_lower.startswith("ml_pure_hold:"):
                row["decision_reason_code"] = reason_lower.split(":", 1)[1]
        rows.append(row)

    ce_entries = 0
    pe_entries = 0
    hold_count = 0
    hold_reasons: dict[str, int] = {}
    edge_values: list[float] = []
    confidence_values: list[float] = []
    for row in rows:
        signal_type = str(row.get("signal_type") or "")
        direction = str(row.get("direction") or "")
        if signal_type == "ENTRY":
            if direction == "CE":
                ce_entries += 1
            elif direction == "PE":
                pe_entries += 1
        elif signal_type == "HOLD":
            hold_count += 1
            code = str(row.get("decision_reason_code") or "").strip().lower() or "unknown"
            hold_reasons[code] = int(hold_reasons.get(code, 0) + 1)

        metrics = row.get("decision_metrics") if isinstance(row.get("decision_metrics"), dict) else {}
        edge = _safe_float(metrics.get("edge"))
        if edge is None:
            ce_prob = _safe_float(metrics.get("ce_prob"))
            pe_prob = _safe_float(metrics.get("pe_prob"))
            if ce_prob is None:
                ce_prob = _safe_float(row.get("ml_ce_prob"))
            if pe_prob is None:
                pe_prob = _safe_float(row.get("ml_pe_prob"))
            if ce_prob is not None and pe_prob is not None:
                edge = abs(float(ce_prob) - float(pe_prob))
        if edge is not None:
            edge_values.append(float(edge))
        conf = _safe_float(metrics.get("confidence"))
        if conf is None:
            conf = _safe_float(row.get("confidence"))
        if conf is not None:
            confidence_values.append(float(conf))

    directional_total = int(ce_entries + pe_entries)
    row_total = int(len(rows))
    low_edge_conflict_holds = int(hold_reasons.get("low_edge_conflict", 0))
    feature_stale_holds = int(hold_reasons.get("feature_stale", 0))
    feature_incomplete_holds = int(hold_reasons.get("feature_incomplete", 0))
    status = "ML_PURE_ACTIVE_TODAY" if row_total > 0 else "NO_ML_PURE_EVIDENCE"
    summary = (
        f"ML-pure processed {row_total} decision event(s) with {ce_entries} CE, {pe_entries} PE, {hold_count} HOLD."
        if row_total > 0
        else "No ML-pure decision events found in sampled window."
    )
    rolling_window_raw = str(os.getenv("ML_PURE_MONITOR_WINDOW_TRADE_DAYS") or "30").strip()
    try:
        rolling_window = int(rolling_window_raw)
    except Exception:
        rolling_window = 30
    rolling_window = max(5, rolling_window)
    rolling_quality_status = "unavailable"
    rolling_quality_reason = None
    rolling_quality = None
    if positions_coll is None:
        rolling_quality_reason = "positions_collection_missing"
    elif not callable(_rolling_ml_quality_from_collections):
        rolling_quality_reason = "rolling_quality_evaluator_unavailable"
    else:
        try:
            rolling_quality = _rolling_ml_quality_from_collections(
                signals_coll,
                positions_coll,
                date_to=str(date_ist),
                window_trade_days=rolling_window,
                threshold_report_path=os.getenv("ML_PURE_THRESHOLD_REPORT"),
                training_summary_path=os.getenv("ML_PURE_TRAINING_SUMMARY_PATH"),
            )
            rolling_quality_status = "ok"
        except Exception as exc:
            rolling_quality_status = "error"
            rolling_quality = {
                "stage1_precision": {"available": False},
                "profit_factor": {"available": False},
                "regime_drift": {"available": False},
                "breaches": {},
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
    if rolling_quality is None:
        rolling_quality = {
            "stage1_precision": {"available": False},
            "profit_factor": {"available": False},
            "regime_drift": {"available": False},
            "breaches": {},
        }
    if isinstance(rolling_quality, dict):
        rolling_quality["status"] = rolling_quality_status
        rolling_quality["window_trade_days"] = rolling_window
        if rolling_quality_reason:
            rolling_quality["reason"] = rolling_quality_reason
    if isinstance(rolling_quality, dict):
        stage1_quality = (rolling_quality.get("stage1_precision") or {}) if isinstance(rolling_quality, dict) else {}
        precision = _safe_float(((rolling_quality.get("stage1_precision") or {}).get("precision")))
        profit_factor = _safe_float(((rolling_quality.get("profit_factor") or {}).get("profit_factor")))
        rolling_error = rolling_quality.get("error") if isinstance(rolling_quality.get("error"), dict) else None
        if precision is not None or profit_factor is not None or stage1_quality.get("available") is False or rolling_error is not None:
            extras: list[str] = []
            if precision is not None:
                extras.append(f"30d precision {precision * 100.0:.1f}%")
            elif stage1_quality.get("available") is False:
                extras.append("30d precision unavailable")
            if profit_factor is not None:
                extras.append(f"30d PF {profit_factor:.2f}")
            if rolling_error is not None:
                extras.append("30d monitoring error")
            elif str(rolling_quality.get("status") or "") == "unavailable":
                extras.append("30d monitoring unavailable")
            if extras:
                summary = f"{summary} {' | '.join(extras)}."
    return {
        "status": status,
        "summary": summary,
        "counts": {
            "sampled_rows": row_total,
            "entries_ce": int(ce_entries),
            "entries_pe": int(pe_entries),
            "holds": int(hold_count),
            "directional_entries": directional_total,
            "holds_low_edge_conflict": low_edge_conflict_holds,
            "holds_feature_stale": feature_stale_holds,
            "holds_feature_incomplete": feature_incomplete_holds,
        },
        "ratios": {
            "hold_rate": _safe_ratio(hold_count, row_total),
            "low_edge_conflict_rate": _safe_ratio(low_edge_conflict_holds, hold_count),
            "feature_stale_hold_rate": _safe_ratio(feature_stale_holds, hold_count),
            "feature_incomplete_hold_rate": _safe_ratio(feature_incomplete_holds, hold_count),
            "ce_vs_pe_skew": _safe_ratio(ce_entries, directional_total),
        },
        "hold_reasons": hold_reasons,
        "edge_distribution": _distribution(edge_values),
        "confidence_distribution": _distribution(confidence_values),
        "rolling_quality": rolling_quality,
        "latest_decision": rows[0] if rows else None,
        "recent_decisions": rows[:25],
    }


__all__ = [
    "build_ml_pure_diagnostics",
]
