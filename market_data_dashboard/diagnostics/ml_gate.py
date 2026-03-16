from __future__ import annotations

import os
from typing import Any, Optional

from contracts_app.strategy_decision_contract import (
    normalize_decision_mode,
    normalize_engine_mode,
    parse_metric_token,
)

try:
    from ..strategy_evaluation_service import _iso_or_none, _safe_float
except ImportError:
    from strategy_evaluation_service import _iso_or_none, _safe_float  # type: ignore


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


def _safe_ratio(numerator: Any, denominator: Any) -> Optional[float]:
    try:
        num = float(numerator)
        den = float(denominator)
    except Exception:
        return None
    if den <= 0:
        return None
    return num / den


def policy_row_from_vote_doc(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    vote = ((doc.get("payload") or {}).get("vote")) if isinstance(doc.get("payload"), dict) else {}
    vote = vote if isinstance(vote, dict) else {}
    raw_signals = vote.get("raw_signals") if isinstance(vote.get("raw_signals"), dict) else {}
    raw_signals = raw_signals if isinstance(raw_signals, dict) else {}
    policy_reason = str(raw_signals.get("_policy_reason") or "").strip() or None
    policy_checks = raw_signals.get("_policy_checks") if isinstance(raw_signals.get("_policy_checks"), dict) else {}
    policy_checks = policy_checks if isinstance(policy_checks, dict) else {}
    policy_allowed = _coerce_bool(raw_signals.get("_policy_allowed"))
    policy_score = _safe_float(raw_signals.get("_policy_score"))
    warmup_blocked = bool(raw_signals.get("_entry_warmup_blocked"))
    warmup_reason = str(raw_signals.get("_entry_warmup_reason") or "").strip() or None
    if policy_reason is None and not policy_checks and not warmup_blocked:
        return None

    ml_keys = sorted([str(key) for key in policy_checks.keys() if str(key).startswith("ml_")])
    ml_applied = bool(ml_keys) or (policy_reason is not None and policy_reason.lower().startswith("ml:"))
    ml_score_calibrated = parse_metric_token(policy_checks.get("ml_score_calibrated"), "score")
    ml_threshold = parse_metric_token(policy_checks.get("ml_threshold"), "threshold")
    engine_mode = normalize_engine_mode(doc.get("engine_mode") or vote.get("engine_mode"))
    decision_mode = normalize_decision_mode(
        doc.get("decision_mode")
        or vote.get("decision_mode")
        or ("ml_gate" if ml_applied else "rule_vote")
    )
    decision_reason_code = str(
        doc.get("decision_reason_code")
        or vote.get("decision_reason_code")
        or ("policy_allowed" if policy_allowed else ("policy_block" if policy_reason else ""))
    ).strip() or None
    decision_metrics = doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else {}
    if not decision_metrics and isinstance(vote.get("decision_metrics"), dict):
        decision_metrics = dict(vote.get("decision_metrics") or {})
    return {
        "timestamp": _iso_or_none(doc.get("timestamp") or vote.get("timestamp")),
        "snapshot_id": str(doc.get("snapshot_id") or vote.get("snapshot_id") or "").strip() or None,
        "strategy": str(doc.get("strategy") or vote.get("strategy") or "").strip() or None,
        "signal_type": str(doc.get("signal_type") or vote.get("signal_type") or "").strip() or None,
        "direction": str(doc.get("direction") or vote.get("direction") or "").strip() or None,
        "vote_confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else vote.get("confidence")),
        "vote_reason": str(doc.get("reason") or vote.get("reason") or "").strip() or None,
        "policy_allowed": policy_allowed,
        "policy_score": policy_score,
        "policy_reason": policy_reason,
        "policy_checks": policy_checks,
        "ml_applied": ml_applied,
        "ml_keys": ml_keys,
        "ml_score_calibrated": ml_score_calibrated,
        "ml_threshold": ml_threshold,
        "engine_mode": engine_mode,
        "decision_mode": decision_mode,
        "decision_reason_code": decision_reason_code,
        "decision_metrics": decision_metrics,
        "strategy_family_version": str(
            doc.get("strategy_family_version") or vote.get("strategy_family_version") or ""
        ).strip()
        or None,
        "strategy_profile_id": str(
            doc.get("strategy_profile_id") or vote.get("strategy_profile_id") or ""
        ).strip()
        or None,
        "warmup_blocked": warmup_blocked,
        "warmup_reason": warmup_reason,
    }


def build_ml_gate_diagnostics(*, date_ist: str, votes_coll: Any) -> dict[str, Any]:
    lookback_raw = str(os.getenv("LIVE_STRATEGY_ML_DIAG_LOOKBACK") or "250").strip()
    try:
        lookback = int(lookback_raw)
    except Exception:
        lookback = 250
    lookback = max(25, min(1000, lookback))

    day_query = {"trade_date_ist": str(date_ist)}
    projection = {
        "_id": 0,
        "timestamp": 1,
        "snapshot_id": 1,
        "strategy": 1,
        "signal_type": 1,
        "direction": 1,
        "confidence": 1,
        "reason": 1,
        "payload.vote": 1,
    }
    recent_docs = list(votes_coll.find(day_query, projection).sort("timestamp", -1).limit(int(lookback)))
    policy_rows: list[dict[str, Any]] = []
    for doc in recent_docs:
        row = policy_row_from_vote_doc(doc)
        if row is not None:
            policy_rows.append(row)
    ml_rows = [row for row in policy_rows if bool(row.get("ml_applied"))]

    directional_entry_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "signal_type": "ENTRY", "direction": {"$in": ["CE", "PE"]}}
    )
    policy_evaluated_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._policy_reason": {"$exists": True}}
    )
    ml_policy_votes_day = votes_coll.count_documents(
        {
            "trade_date_ist": str(date_ist),
            "payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True},
        }
    )
    base_allowed_votes_day = votes_coll.count_documents(
        {
            "trade_date_ist": str(date_ist),
            "payload.vote.raw_signals._policy_reason": {"$exists": True},
            "$or": [
                {"payload.vote.raw_signals._policy_reason": {"$regex": "^allowed score=", "$options": "i"}},
                {"payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True}},
            ],
        }
    )
    ml_allowed_votes_day = votes_coll.count_documents(
        {
            "trade_date_ist": str(date_ist),
            "payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True},
            "payload.vote.raw_signals._policy_allowed": True,
        }
    )
    ml_blocked_votes_day = votes_coll.count_documents(
        {
            "trade_date_ist": str(date_ist),
            "payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True},
            "payload.vote.raw_signals._policy_allowed": False,
        }
    )
    warmup_blocked_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._entry_warmup_blocked": True}
    )
    ml_scored_to_base_allowed_ratio = _safe_ratio(ml_policy_votes_day, base_allowed_votes_day)
    ml_block_rate_day = _safe_ratio(ml_blocked_votes_day, ml_policy_votes_day)
    ml_pass_rate_day = _safe_ratio(ml_allowed_votes_day, ml_policy_votes_day)

    latest_ml_day_doc = votes_coll.find_one(
        {
            "trade_date_ist": str(date_ist),
            "payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True},
        },
        projection,
        sort=[("timestamp", -1)],
    )
    latest_ml_any_doc = votes_coll.find_one(
        {"payload.vote.raw_signals._policy_checks.ml_score_calibrated": {"$exists": True}},
        projection,
        sort=[("timestamp", -1)],
    )
    latest_ml_day = policy_row_from_vote_doc(latest_ml_day_doc) if isinstance(latest_ml_day_doc, dict) else None
    latest_ml_any = policy_row_from_vote_doc(latest_ml_any_doc) if isinstance(latest_ml_any_doc, dict) else None
    latest_policy = policy_rows[0] if policy_rows else None

    ml_registry_env = str(
        os.getenv("ML_ENTRY_REGISTRY") or os.getenv("STRATEGY_ML_ENTRY_REGISTRY") or ""
    ).strip() or None
    ml_experiment_env = str(
        os.getenv("ML_ENTRY_EXPERIMENT_ID") or os.getenv("STRATEGY_ML_ENTRY_EXPERIMENT_ID") or ""
    ).strip() or None
    ml_threshold_policy_env = str(
        os.getenv("ML_ENTRY_THRESHOLD_POLICY") or os.getenv("STRATEGY_ML_ENTRY_THRESHOLD_POLICY") or ""
    ).strip() or None
    ml_env_config_present = bool(ml_registry_env and ml_experiment_env)

    if ml_policy_votes_day > 0:
        status = "ML_ACTIVE_TODAY"
        summary = f"ML evaluated {int(ml_policy_votes_day)} directional entry vote(s) today."
    elif directional_entry_votes_day <= 0:
        status = "NO_DIRECTIONAL_ENTRY_VOTES_TODAY"
        summary = "No CE/PE ENTRY votes today; ML gate was not reached."
    elif policy_evaluated_votes_day <= 0 and warmup_blocked_votes_day > 0:
        status = "ENTRY_WARMUP_BLOCKED"
        summary = f"Entry warmup blocked {int(warmup_blocked_votes_day)} vote(s); policy gate not reached."
    elif policy_evaluated_votes_day <= 0:
        status = "NO_POLICY_EVALUATION_TODAY"
        summary = "Directional entries existed but no policy decisions were persisted."
    elif latest_ml_any is not None:
        status = "NO_ML_SAMPLE_TODAY"
        summary = "Policy ran today, but no vote reached ML scoring (base gate may have blocked first)."
    else:
        status = "NO_ML_EVIDENCE"
        summary = "No ML-scored votes found in persisted history."

    return {
        "status": status,
        "summary": summary,
        "env_ml_config_present": ml_env_config_present,
        "env_ml_registry": ml_registry_env,
        "env_ml_experiment_id": ml_experiment_env,
        "env_ml_threshold_policy": ml_threshold_policy_env,
        "counts": {
            "directional_entry_votes_day": int(directional_entry_votes_day),
            "policy_evaluated_votes_day": int(policy_evaluated_votes_day),
            "base_allowed_votes_day": int(base_allowed_votes_day),
            "ml_policy_votes_day": int(ml_policy_votes_day),
            "ml_allowed_votes_day": int(ml_allowed_votes_day),
            "ml_blocked_votes_day": int(ml_blocked_votes_day),
            "warmup_blocked_votes_day": int(warmup_blocked_votes_day),
            "sampled_votes": len(recent_docs),
            "sampled_policy_rows": len(policy_rows),
            "sampled_ml_rows": len(ml_rows),
        },
        "ratios": {
            "ml_scored_to_base_allowed": ml_scored_to_base_allowed_ratio,
            "ml_block_rate_day": ml_block_rate_day,
            "ml_pass_rate_day": ml_pass_rate_day,
        },
        "latest_policy_decision": latest_policy,
        "latest_ml_decision_day": latest_ml_day,
        "latest_ml_decision_any": latest_ml_any,
        "recent_policy_decisions": policy_rows[:20],
        "recent_ml_decisions": ml_rows[:20],
    }


__all__ = [
    "policy_row_from_vote_doc",
    "build_ml_gate_diagnostics",
]
