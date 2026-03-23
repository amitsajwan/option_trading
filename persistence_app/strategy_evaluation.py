from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from pathlib import Path
import re
from statistics import median
from typing import Any, Iterable, Optional

from contracts_app import TimestampSourceMode, isoformat_ist

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None


_REASON_RE = re.compile(r"^\[(?P<regime>[^\]]+)\]\s+(?P<strategy>[^:]+):")
_ML_METRIC_KEYS = (
    "ml_entry_prob",
    "ml_direction_up_prob",
    "ml_ce_prob",
    "ml_pe_prob",
    "ml_recipe_prob",
    "ml_recipe_margin",
)
_CALIBRATION_BUCKETS = (0.50, 0.60, 0.70, 0.80, 1.0000001)


def _mongo_client() -> MongoClient:
    if MongoClient is None:
        raise RuntimeError("pymongo_not_installed")
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    return MongoClient(
        host=str(os.getenv("MONGO_HOST") or "localhost"),
        port=int(os.getenv("MONGO_PORT") or "27017"),
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=5000,
    )


def _date_filter(*, date_from: Optional[str], date_to: Optional[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if date_from:
        values["$gte"] = str(date_from)
    if date_to:
        values["$lte"] = str(date_to)
    return {"trade_date_ist": values} if values else {}


def _parse_reason(reason: str) -> tuple[Optional[str], Optional[str]]:
    match = _REASON_RE.match(str(reason or "").strip())
    if not match:
        return None, None
    regime = str(match.group("regime") or "").strip() or None
    strategy = str(match.group("strategy") or "").strip() or None
    return strategy, regime


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _classify_actual_outcome(*, exit_reason: Any, pnl_pct: Any) -> str:
    reason = str(exit_reason or "").strip().upper()
    pnl = _safe_float(pnl_pct)
    if reason in {"STOP_LOSS", "TRAILING_STOP", "RISK_BREACH"}:
        return "stop"
    if reason == "TIME_STOP":
        return "time"
    if pnl is not None:
        if pnl > 0:
            return "win"
        if pnl < 0:
            return "loss"
    return "unknown"


def _resolved_ml_metrics(doc: dict[str, Any], payload_signal: dict[str, Any]) -> dict[str, Optional[float]]:
    decision_metrics = doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else {}
    if not decision_metrics and isinstance(payload_signal.get("decision_metrics"), dict):
        decision_metrics = dict(payload_signal.get("decision_metrics") or {})
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
    return {
        "ml_entry_prob": _safe_float(
            doc.get("ml_entry_prob") if doc.get("ml_entry_prob") is not None else decision_metrics.get("entry_prob")
        ),
        "ml_direction_up_prob": direction_up,
        "ml_ce_prob": ce_prob,
        "ml_pe_prob": pe_prob,
        "ml_recipe_prob": _safe_float(
            doc.get("ml_recipe_prob") if doc.get("ml_recipe_prob") is not None else decision_metrics.get("recipe_prob")
        ),
        "ml_recipe_margin": _safe_float(
            doc.get("ml_recipe_margin") if doc.get("ml_recipe_margin") is not None else decision_metrics.get("recipe_margin")
        ),
    }


def _iso_or_none(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return isoformat_ist(value, naive_mode=TimestampSourceMode.LEGACY_MONGO_UTC)
    text = str(value or "").strip()
    return text or None


def _read_json_file(path: Optional[str]) -> Optional[dict[str, Any]]:
    text = str(path or "").strip()
    if not text:
        return None
    file_path = Path(text).expanduser()
    if not file_path.exists():
        return None
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_stage1_threshold_meta(threshold_report_path: Optional[str]) -> dict[str, Any]:
    payload = _read_json_file(threshold_report_path or os.getenv("ML_PURE_THRESHOLD_REPORT"))
    if isinstance(payload, dict):
        stage1 = payload.get("stage1")
        if isinstance(stage1, dict):
            selected = _safe_float(stage1.get("selected_threshold"))
            if selected is not None:
                return {
                    "available": True,
                    "threshold": float(selected),
                    "source": "threshold_report.stage1.selected_threshold",
                    "reason": None,
                }
        selected = _safe_float(payload.get("selected_threshold"))
        if selected is not None:
            return {
                "available": True,
                "threshold": float(selected),
                "source": "threshold_report.selected_threshold",
                "reason": None,
            }
    env_override = _safe_float(os.getenv("ML_PURE_STAGE1_THRESHOLD"))
    if env_override is not None:
        return {
            "available": True,
            "threshold": float(env_override),
            "source": "env.ML_PURE_STAGE1_THRESHOLD",
            "reason": None,
        }
    return {
        "available": False,
        "threshold": None,
        "source": None,
        "reason": "missing_threshold_artifact",
    }


def _resolve_env_threshold_meta(env_name: str, default_value: float) -> dict[str, Any]:
    env_override = _safe_float(os.getenv(env_name))
    if env_override is not None:
        return {
            "available": True,
            "value": float(env_override),
            "source": f"env.{env_name}",
            "reason": None,
        }
    return {
        "available": True,
        "value": float(default_value),
        "source": "default",
        "reason": None,
    }


def _rolling_quality_thresholds() -> dict[str, dict[str, Any]]:
    return {
        "stage1_precision_warning": _resolve_env_threshold_meta(
            "LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN",
            0.50,
        ),
        "profit_factor_warning": _resolve_env_threshold_meta(
            "LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN",
            0.90,
        ),
        "regime_drift_info": _resolve_env_threshold_meta(
            "LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO",
            0.20,
        ),
    }


def _normalize_distribution(raw: Any) -> Optional[dict[str, float]]:
    if isinstance(raw, dict):
        parsed: dict[str, float] = {}
        total = 0.0
        for key, value in raw.items():
            number = _safe_float(value)
            if number is None or number < 0:
                continue
            parsed[str(key)] = float(number)
            total += float(number)
        if not parsed:
            return None
        if total > 1.000001:
            return {key: (value / total) for key, value in parsed.items()}
        return parsed
    if isinstance(raw, list):
        counts: dict[str, float] = {}
        total = 0.0
        for item in raw:
            if not isinstance(item, dict):
                continue
            regime = str(item.get("regime") or item.get("name") or "").strip()
            if not regime:
                continue
            share = _safe_float(item.get("share"))
            if share is not None:
                counts[regime] = float(share)
                total += float(share)
                continue
            count = _safe_float(item.get("trades") if item.get("trades") is not None else item.get("count"))
            if count is None or count < 0:
                continue
            counts[regime] = float(count)
            total += float(count)
        if not counts:
            return None
        if total > 1.000001:
            return {key: (value / total) for key, value in counts.items()}
        return counts
    return None


def _load_training_regime_distribution(training_summary_path: Optional[str]) -> Optional[dict[str, float]]:
    payload = _read_json_file(training_summary_path or os.getenv("ML_PURE_TRAINING_SUMMARY_PATH"))
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("training_regime_distribution"),
        payload.get("regime_distribution"),
        payload.get("by_regime"),
    ]
    today_summary = payload.get("today_summary")
    if isinstance(today_summary, dict):
        candidates.append(today_summary.get("by_regime"))
    for candidate in candidates:
        parsed = _normalize_distribution(candidate)
        if parsed:
            return parsed
    return None


def _distribution_from_counts(counts: dict[str, int]) -> dict[str, float]:
    total = float(sum(int(value) for value in counts.values()))
    if total <= 0:
        return {}
    return {str(key): (float(value) / total) for key, value in counts.items()}


def _bucket_label(lower: float, upper: float) -> str:
    upper_display = min(float(upper), 1.0)
    if upper_display >= 1.0:
        return f"{lower:.1f}+"
    return f"{lower:.1f}-{upper_display:.1f}"


def _calibration_buckets(trades: list[dict[str, Any]], *, prob_key: str, direction: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    direction_token = str(direction).strip().upper()
    for idx in range(len(_CALIBRATION_BUCKETS) - 1):
        lower = float(_CALIBRATION_BUCKETS[idx])
        upper = float(_CALIBRATION_BUCKETS[idx + 1])
        selected: list[dict[str, Any]] = []
        probs: list[float] = []
        for trade in trades:
            prob = _safe_float(trade.get(prob_key))
            if prob is None:
                continue
            in_bucket = (prob >= lower) and (prob < upper if idx < len(_CALIBRATION_BUCKETS) - 2 else prob <= upper)
            if not in_bucket:
                continue
            selected.append(trade)
            probs.append(float(prob))
        if not selected:
            continue
        positives = sum(
            1
            for trade in selected
            if str(trade.get("direction") or "").strip().upper() == direction_token
            and str(trade.get("actual_outcome") or "").strip().lower() == "win"
        )
        avg_prob = float(sum(probs) / len(probs))
        actual_rate = float(positives / len(selected))
        midpoint = float((lower + min(upper, 1.0)) / 2.0)
        rows.append(
            {
                "bucket": _bucket_label(lower, upper),
                "samples": int(len(selected)),
                "avg_predicted_prob": avg_prob,
                "bucket_midpoint": midpoint,
                "actual_win_rate": actual_rate,
                "calibration_gap": actual_rate - avg_prob,
            }
        )
    return rows


def _rolling_window_trades(trades: list[dict[str, Any]], *, window_trade_days: int) -> tuple[list[dict[str, Any]], list[str]]:
    unique_days = sorted({str(trade.get("trade_date_ist") or "").strip() for trade in trades if str(trade.get("trade_date_ist") or "").strip()})
    if not unique_days:
        return [], []
    days = unique_days[-max(1, int(window_trade_days)) :]
    day_set = set(days)
    window = [trade for trade in trades if str(trade.get("trade_date_ist") or "").strip() in day_set]
    return window, days


def _rolling_ml_quality_from_trades(
    trades: list[dict[str, Any]],
    *,
    window_trade_days: int,
    stage1_threshold_meta: dict[str, Any],
    training_regime_distribution: Optional[dict[str, float]],
) -> dict[str, Any]:
    window_trades, days = _rolling_window_trades(trades, window_trade_days=window_trade_days)
    ml_trades = [
        trade
        for trade in window_trades
        if str(trade.get("engine_mode") or "").strip().lower() == "ml_pure"
        or any(trade.get(key) is not None for key in _ML_METRIC_KEYS)
    ]
    thresholds = _rolling_quality_thresholds()
    stage1_warning_meta = dict(thresholds["stage1_precision_warning"])
    profit_factor_warning_meta = dict(thresholds["profit_factor_warning"])
    regime_drift_info_meta = dict(thresholds["regime_drift_info"])
    stage1_threshold = _safe_float(stage1_threshold_meta.get("threshold"))
    threshold_available = bool(stage1_threshold_meta.get("available")) and stage1_threshold is not None
    approved = (
        [
            trade
            for trade in ml_trades
            if (_safe_float(trade.get("ml_entry_prob")) is not None)
            and float(_safe_float(trade.get("ml_entry_prob")) or 0.0) >= float(stage1_threshold)
        ]
        if threshold_available
        else []
    )
    approved_wins = sum(1 for trade in approved if str(trade.get("actual_outcome") or "").strip().lower() == "win")
    stage1_precision = (float(approved_wins) / len(approved)) if approved else None

    returns = [_safe_float(trade.get("actual_return_pct")) for trade in ml_trades]
    wins = [float(value) for value in returns if value is not None and value > 0]
    losses = [float(value) for value in returns if value is not None and value < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    live_regime_counts: dict[str, int] = {}
    for trade in ml_trades:
        regime = str(trade.get("regime") or "").strip() or "UNKNOWN"
        live_regime_counts[regime] = int(live_regime_counts.get(regime, 0) + 1)
    live_regime_distribution = _distribution_from_counts(live_regime_counts)
    baseline = dict(training_regime_distribution or {})
    all_regimes = sorted(set(live_regime_distribution) | set(baseline))
    regime_rows: list[dict[str, Any]] = []
    max_shift = 0.0
    for regime in all_regimes:
        live_share = live_regime_distribution.get(regime)
        training_share = baseline.get(regime)
        abs_shift = abs(float(live_share) - float(training_share)) if live_share is not None and training_share is not None else None
        if abs_shift is not None:
            max_shift = max(max_shift, float(abs_shift))
        regime_rows.append(
            {
                "regime": regime,
                "live_share": live_share,
                "training_share": training_share,
                "abs_shift": abs_shift,
            }
        )
    stage1_warning_threshold = _safe_float(stage1_warning_meta.get("value"))
    profit_factor_warning_threshold = _safe_float(profit_factor_warning_meta.get("value"))
    regime_drift_info_threshold = _safe_float(regime_drift_info_meta.get("value"))
    stage1_precision_warning = bool(
        threshold_available
        and stage1_precision is not None
        and stage1_warning_threshold is not None
        and stage1_precision < stage1_warning_threshold
    )
    profit_factor_warning = bool(
        profit_factor is not None
        and profit_factor_warning_threshold is not None
        and profit_factor < profit_factor_warning_threshold
    )
    regime_drift_info = bool(
        baseline
        and (float(max_shift) if regime_rows else None) is not None
        and regime_drift_info_threshold is not None
        and float(max_shift) > regime_drift_info_threshold
    )

    return {
        "window_trade_days": int(window_trade_days),
        "window_dates": {
            "date_from": (days[0] if days else None),
            "date_to": (days[-1] if days else None),
            "days_total": int(len(days)),
        },
        "counts": {
            "closed_trades": int(len(window_trades)),
            "ml_closed_trades": int(len(ml_trades)),
            "stage1_approved_trades": int(len(approved)),
        },
        "stage1_precision": {
            "available": threshold_available,
            "threshold": (float(stage1_threshold) if threshold_available and stage1_threshold is not None else None),
            "source": stage1_threshold_meta.get("source"),
            "reason": stage1_threshold_meta.get("reason"),
            "warning_threshold": stage1_warning_threshold,
            "warning_threshold_source": stage1_warning_meta.get("source"),
            "trades_considered": int(len(approved)),
            "wins": int(approved_wins),
            "precision": stage1_precision,
        },
        "stage2_ce_calibration": {
            "buckets": _calibration_buckets(ml_trades, prob_key="ml_ce_prob", direction="CE"),
        },
        "stage2_pe_calibration": {
            "buckets": _calibration_buckets(ml_trades, prob_key="ml_pe_prob", direction="PE"),
        },
        "profit_factor": {
            "gross_profit_pct": gross_profit,
            "gross_loss_pct": (-gross_loss if gross_loss > 0 else 0.0),
            "profit_factor": profit_factor,
            "warning_threshold": profit_factor_warning_threshold,
            "warning_threshold_source": profit_factor_warning_meta.get("source"),
        },
        "regime_drift": {
            "training_distribution": (baseline or None),
            "live_distribution": live_regime_distribution,
            "rows": regime_rows,
            "max_abs_shift": (float(max_shift) if regime_rows and baseline else None),
            "info_threshold": regime_drift_info_threshold,
            "info_threshold_source": regime_drift_info_meta.get("source"),
        },
        "thresholds": thresholds,
        "breaches": {
            "stage1_precision_warning": stage1_precision_warning,
            "profit_factor_warning": profit_factor_warning,
            "regime_drift_info": regime_drift_info,
        },
    }


def _load_signal_map(signal_coll: Any, *, date_match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projection = {
        "_id": 0,
        "signal_id": 1,
        "regime": 1,
        "confidence": 1,
        "reason": 1,
        "timestamp": 1,
        "engine_mode": 1,
        "decision_metrics": 1,
        "ml_entry_prob": 1,
        "ml_direction_up_prob": 1,
        "ml_ce_prob": 1,
        "ml_pe_prob": 1,
        "ml_recipe_prob": 1,
        "ml_recipe_margin": 1,
        "payload.signal": 1,
        "trade_date_ist": 1,
    }
    output: dict[str, dict[str, Any]] = {}
    for doc in signal_coll.find(date_match, projection):
        signal_id = str(doc.get("signal_id") or "").strip()
        if not signal_id:
            continue
        payload_signal = (((doc.get("payload") or {}).get("signal")) if isinstance(doc.get("payload"), dict) else {}) or {}
        if not isinstance(payload_signal, dict):
            payload_signal = {}
        decision_metrics = doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else {}
        if not decision_metrics and isinstance(payload_signal.get("decision_metrics"), dict):
            decision_metrics = dict(payload_signal.get("decision_metrics") or {})
        output[signal_id] = {
            "signal_id": signal_id,
            "regime": str(doc.get("regime") or payload_signal.get("regime") or "").strip() or None,
            "confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence")),
            "reason": str(doc.get("reason") or payload_signal.get("reason") or "").strip(),
            "contributing_strategies": list(payload_signal.get("contributing_strategies") or []),
            "timestamp": _iso_or_none(payload_signal.get("timestamp") or doc.get("timestamp")),
            "trade_date_ist": str(doc.get("trade_date_ist") or "").strip() or None,
            "engine_mode": str(doc.get("engine_mode") or payload_signal.get("engine_mode") or "").strip() or None,
            "decision_metrics": decision_metrics,
            **_resolved_ml_metrics(doc, payload_signal),
        }
    return output


def _load_positions(position_coll: Any, *, date_match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projection = {
        "_id": 0,
        "position_id": 1,
        "signal_id": 1,
        "event": 1,
        "timestamp": 1,
        "trade_date_ist": 1,
        "engine_mode": 1,
        "decision_metrics": 1,
        "ml_entry_prob": 1,
        "ml_direction_up_prob": 1,
        "ml_ce_prob": 1,
        "ml_pe_prob": 1,
        "ml_recipe_prob": 1,
        "ml_recipe_margin": 1,
        "actual_outcome": 1,
        "actual_return_pct": 1,
        "payload.position": 1,
    }
    positions: dict[str, dict[str, Any]] = {}
    for doc in position_coll.find(date_match, projection).sort("timestamp", 1):
        position_id = str(doc.get("position_id") or "").strip()
        if not position_id:
            continue
        payload_position = (((doc.get("payload") or {}).get("position")) if isinstance(doc.get("payload"), dict) else {}) or {}
        if not isinstance(payload_position, dict):
            payload_position = {}
        entry = positions.setdefault(position_id, {"position_id": position_id})
        event = str(doc.get("event") or payload_position.get("event") or "").strip().upper()
        if event == "POSITION_OPEN":
            entry["open"] = payload_position
            entry["open_doc"] = doc
        elif event == "POSITION_CLOSE":
            entry["close"] = payload_position
            entry["close_doc"] = doc
    return positions


def _primary_strategy(*, signal_doc: dict[str, Any], open_position: dict[str, Any]) -> Optional[str]:
    reason_strategy, _ = _parse_reason(str(signal_doc.get("reason") or open_position.get("reason") or ""))
    if reason_strategy:
        return reason_strategy
    strategies = signal_doc.get("contributing_strategies")
    if isinstance(strategies, list) and strategies:
        first = str(strategies[0] or "").strip()
        return first or None
    return None


def _merge_ml_metrics(*sources: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Optional[float]]:
    merged = {key: None for key in _ML_METRIC_KEYS}
    for doc, payload in sources:
        resolved = _resolved_ml_metrics(doc, payload)
        for key, value in resolved.items():
            if merged.get(key) is None and value is not None:
                merged[key] = value
    return merged


def _trade_from_docs(position_id: str, docs: dict[str, Any], signal_map: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    open_position = docs.get("open")
    close_position = docs.get("close")
    if not isinstance(open_position, dict) or not isinstance(close_position, dict):
        return None

    open_doc = docs.get("open_doc") if isinstance(docs.get("open_doc"), dict) else {}
    close_doc = docs.get("close_doc") if isinstance(docs.get("close_doc"), dict) else {}
    signal_id = str(open_position.get("signal_id") or open_doc.get("signal_id") or close_doc.get("signal_id") or "").strip()
    signal_doc = signal_map.get(signal_id, {})
    strategy = _primary_strategy(signal_doc=signal_doc, open_position=open_position)
    _, regime_from_reason = _parse_reason(str(signal_doc.get("reason") or open_position.get("reason") or ""))
    regime = str(signal_doc.get("regime") or regime_from_reason or "").strip() or None

    pnl_pct = _safe_float(close_position.get("pnl_pct"))
    mfe_pct = _safe_float(close_position.get("mfe_pct"))
    mae_pct = _safe_float(close_position.get("mae_pct"))
    entry_premium = _safe_float(open_position.get("entry_premium"))
    exit_premium = _safe_float(close_position.get("exit_premium"))
    bars_held = int(float(close_position.get("bars_held") or 0))
    lots = int(float(open_position.get("lots") or 0)) if open_position.get("lots") is not None else None
    stop_loss_pct = _safe_float(open_position.get("stop_loss_pct"))
    target_pct = _safe_float(open_position.get("target_pct"))
    confidence = _safe_float(signal_doc.get("confidence"))
    engine_mode = str(signal_doc.get("engine_mode") or open_doc.get("engine_mode") or close_doc.get("engine_mode") or "").strip() or None
    ml_metrics = _merge_ml_metrics(
        (signal_doc, {}),
        (open_doc, open_position),
        (close_doc, close_position),
    )

    result = "UNKNOWN"
    if pnl_pct is not None:
        if pnl_pct > 0:
            result = "WIN"
        elif pnl_pct < 0:
            result = "LOSS"
        else:
            result = "FLAT"

    actual_outcome = str(close_doc.get("actual_outcome") or "").strip().lower() or _classify_actual_outcome(
        exit_reason=close_position.get("exit_reason"),
        pnl_pct=pnl_pct,
    )
    actual_return_pct = _safe_float(close_doc.get("actual_return_pct"))
    if actual_return_pct is None:
        actual_return_pct = pnl_pct

    return {
        "position_id": position_id,
        "signal_id": signal_id or None,
        "engine_mode": engine_mode,
        "entry_strategy": strategy,
        "regime": regime,
        "direction": str(open_position.get("direction") or "").strip() or None,
        "entry_time": _iso_or_none(open_position.get("timestamp")),
        "exit_time": _iso_or_none(close_position.get("timestamp")),
        "trade_date_ist": str((open_doc or {}).get("trade_date_ist") or (close_doc or {}).get("trade_date_ist") or "").strip() or None,
        "entry_premium": entry_premium,
        "exit_premium": exit_premium,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "bars_held": bars_held,
        "lots": lots,
        "stop_loss_pct": stop_loss_pct,
        "target_pct": target_pct,
        "signal_confidence": confidence,
        "exit_reason": str(close_position.get("exit_reason") or "").strip() or None,
        "result": result,
        "entry_reason": str(open_position.get("reason") or "").strip() or None,
        "actual_outcome": actual_outcome,
        "actual_return_pct": actual_return_pct,
        **ml_metrics,
    }


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [trade["pnl_pct"] for trade in trades if trade.get("pnl_pct") is not None]
    mfes = [trade["mfe_pct"] for trade in trades if trade.get("mfe_pct") is not None]
    maes = [trade["mae_pct"] for trade in trades if trade.get("mae_pct") is not None]
    bars = [trade["bars_held"] for trade in trades if trade.get("bars_held") is not None]
    confidences = [trade["signal_confidence"] for trade in trades if trade.get("signal_confidence") is not None]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    flats = [value for value in pnls if value == 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    avg_mae_abs = abs(sum(maes) / len(maes)) if maes else None
    avg_mfe = (sum(mfes) / len(mfes)) if mfes else None
    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None

    return {
        "trades": len(trades),
        "wins": len(winners),
        "losses": len(losers),
        "flats": len(flats),
        "win_rate": (len(winners) / len(pnls)) if pnls else None,
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "median_pnl_pct": median(pnls) if pnls else None,
        "avg_winner_pct": (sum(winners) / len(winners)) if winners else None,
        "avg_loser_pct": (sum(losers) / len(losers)) if losers else None,
        "gross_profit_pct": gross_profit if winners else 0.0,
        "gross_loss_pct": -gross_loss if losers else 0.0,
        "profit_factor": profit_factor,
        "expectancy_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": (sum(maes) / len(maes)) if maes else None,
        "mfe_mae_ratio": ((avg_mfe / avg_mae_abs) if avg_mfe is not None and avg_mae_abs not in (None, 0.0) else None),
        "avg_bars_held": (sum(bars) / len(bars)) if bars else None,
        "avg_signal_confidence": (sum(confidences) / len(confidences)) if confidences else None,
    }


def _group_summary(trades: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for trade in trades:
        key = tuple(trade.get(name) for name in keys)
        grouped.setdefault(key, []).append(trade)

    rows: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items(), key=lambda item: tuple("" if value is None else str(value) for value in item[0])):
        row = {name: key[idx] for idx, name in enumerate(keys)}
        row.update(_summarize_trades(items))
        rows.append(row)
    return rows


def _exit_reason_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason") or "UNKNOWN")
        grouped.setdefault(reason, []).append(trade)
    rows: list[dict[str, Any]] = []
    for reason, items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        pnls = [trade["pnl_pct"] for trade in items if trade.get("pnl_pct") is not None]
        rows.append(
            {
                "exit_reason": reason,
                "trades": len(items),
                "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
            }
        )
    return rows


def rolling_ml_quality_from_collections(
    signal_coll: Any,
    position_coll: Any,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    window_trade_days: int = 30,
    threshold_report_path: Optional[str] = None,
    training_summary_path: Optional[str] = None,
) -> dict[str, Any]:
    date_match = _date_filter(date_from=date_from, date_to=date_to)
    signal_map = _load_signal_map(signal_coll, date_match=date_match)
    position_map = _load_positions(position_coll, date_match=date_match)
    trades = [
        trade
        for position_id, docs in position_map.items()
        for trade in [_trade_from_docs(position_id, docs, signal_map)]
        if trade is not None
    ]
    trades.sort(key=lambda item: (str(item.get("entry_time") or ""), str(item.get("position_id") or "")))
    stage1_threshold_meta = _resolve_stage1_threshold_meta(threshold_report_path)
    return _rolling_ml_quality_from_trades(
        trades,
        window_trade_days=int(window_trade_days),
        stage1_threshold_meta=stage1_threshold_meta,
        training_regime_distribution=_load_training_regime_distribution(training_summary_path),
    )


def rolling_ml_quality(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    window_trade_days: int = 30,
    threshold_report_path: Optional[str] = None,
    training_summary_path: Optional[str] = None,
) -> dict[str, Any]:
    client = _mongo_client()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    signal_coll_name = str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals").strip() or "trade_signals"
    position_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions").strip() or "strategy_positions"
    db = client[db_name]
    try:
        return rolling_ml_quality_from_collections(
            db[signal_coll_name],
            db[position_coll_name],
            date_from=date_from,
            date_to=date_to,
            window_trade_days=window_trade_days,
            threshold_report_path=threshold_report_path,
            training_summary_path=training_summary_path,
        )
    finally:
        client.close()


def build_evaluation(
    *,
    date_from: Optional[str],
    date_to: Optional[str],
    limit: int,
    window_trade_days: int = 30,
    threshold_report_path: Optional[str] = None,
    training_summary_path: Optional[str] = None,
) -> dict[str, Any]:
    client = _mongo_client()
    try:
        db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
        vote_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes").strip() or "strategy_votes"
        signal_coll_name = str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals").strip() or "trade_signals"
        position_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions").strip() or "strategy_positions"

        db = client[db_name]
        votes = db[vote_coll_name]
        signals = db[signal_coll_name]
        positions = db[position_coll_name]
        date_match = _date_filter(date_from=date_from, date_to=date_to)

        signal_map = _load_signal_map(signals, date_match=date_match)
        position_map = _load_positions(positions, date_match=date_match)
        trades = [
            trade
            for position_id, docs in position_map.items()
            for trade in [_trade_from_docs(position_id, docs, signal_map)]
            if trade is not None
        ]
        trades.sort(key=lambda item: (str(item.get("entry_time") or ""), str(item.get("position_id") or "")))

        open_positions = [
            {
                "position_id": position_id,
                "has_open": isinstance(docs.get("open"), dict),
                "has_close": isinstance(docs.get("close"), dict),
            }
            for position_id, docs in sorted(position_map.items())
            if not (isinstance(docs.get("open"), dict) and isinstance(docs.get("close"), dict))
        ]

        return {
            "generated_at": isoformat_ist(),
            "db": db_name,
            "collections": {
                "strategy_votes": vote_coll_name,
                "trade_signals": signal_coll_name,
                "strategy_positions": position_coll_name,
            },
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
            },
            "counts": {
                "votes": votes.count_documents(date_match),
                "signals": signals.count_documents(date_match),
                "position_events": positions.count_documents(date_match),
                "closed_trades": len(trades),
                "incomplete_positions": len(open_positions),
            },
            "overall": _summarize_trades(trades),
            "by_strategy_regime": _group_summary(trades, ["entry_strategy", "regime"]),
            "by_strategy": _group_summary(trades, ["entry_strategy"]),
            "by_regime": _group_summary(trades, ["regime"]),
            "by_direction": _group_summary(trades, ["direction"]),
            "by_exit_reason": _exit_reason_summary(trades),
            "rolling_ml_quality": _rolling_ml_quality_from_trades(
                trades,
                window_trade_days=int(window_trade_days),
                stage1_threshold_meta=_resolve_stage1_threshold_meta(threshold_report_path),
                training_regime_distribution=_load_training_regime_distribution(training_summary_path),
            ),
            "incomplete_positions": open_positions[: max(1, int(limit))],
            "latest_trades": trades[-max(1, int(limit)) :],
        }
    finally:
        client.close()


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Performance evaluation from persisted strategy history")
    parser.add_argument("--date-from", default=None, help="Inclusive IST trade_date_ist lower bound YYYY-MM-DD")
    parser.add_argument("--date-to", default=None, help="Inclusive IST trade_date_ist upper bound YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=10, help="Latest trades and incomplete positions to include")
    parser.add_argument("--window-trade-days", type=int, default=30, help="Rolling ML quality window in trading days")
    parser.add_argument("--threshold-report-path", default=None, help="Optional staged threshold_report.json path")
    parser.add_argument("--training-summary-path", default=None, help="Optional staged training summary.json path")
    parser.add_argument("--output", default=None, help="Optional path to write the JSON evaluation report")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = build_evaluation(
        date_from=args.date_from,
        date_to=args.date_to,
        limit=int(args.limit),
        window_trade_days=int(args.window_trade_days),
        threshold_report_path=args.threshold_report_path,
        training_summary_path=args.training_summary_path,
    )
    rendered = json.dumps(report, ensure_ascii=False, default=str, indent=2)
    if args.output:
        output_path = Path(str(args.output)).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
