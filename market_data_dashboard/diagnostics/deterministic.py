from __future__ import annotations

from typing import Any, Optional

from contracts_app.strategy_decision_contract import normalize_decision_mode, normalize_engine_mode

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

    engine_mode = normalize_engine_mode(doc.get("engine_mode") or vote.get("engine_mode"))
    decision_mode = normalize_decision_mode(doc.get("decision_mode") or vote.get("decision_mode") or "rule_vote")
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


def build_deterministic_diagnostics(*, date_ist: str, votes_coll: Any) -> dict[str, Any]:
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
    recent_docs = list(votes_coll.find(day_query, projection).sort("timestamp", -1).limit(250))
    policy_rows: list[dict[str, Any]] = []
    for doc in recent_docs:
        row = policy_row_from_vote_doc(doc)
        if row is not None:
            policy_rows.append(row)

    directional_entry_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "signal_type": "ENTRY", "direction": {"$in": ["CE", "PE"]}}
    )
    policy_evaluated_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._policy_reason": {"$exists": True}}
    )
    policy_allowed_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._policy_allowed": True}
    )
    policy_blocked_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._policy_allowed": False}
    )
    warmup_blocked_votes_day = votes_coll.count_documents(
        {"trade_date_ist": str(date_ist), "payload.vote.raw_signals._entry_warmup_blocked": True}
    )
    policy_pass_rate_day = _safe_ratio(policy_allowed_votes_day, policy_evaluated_votes_day)
    policy_block_rate_day = _safe_ratio(policy_blocked_votes_day, policy_evaluated_votes_day)
    warmup_block_rate_day = _safe_ratio(warmup_blocked_votes_day, directional_entry_votes_day)

    latest_policy = policy_rows[0] if policy_rows else None

    if policy_evaluated_votes_day > 0:
        status = "POLICY_ACTIVE_TODAY"
        summary = f"Deterministic policy evaluated {int(policy_evaluated_votes_day)} vote(s) today."
    elif directional_entry_votes_day <= 0:
        status = "NO_DIRECTIONAL_ENTRY_VOTES_TODAY"
        summary = "No CE/PE ENTRY votes today; deterministic policy was not reached."
    elif warmup_blocked_votes_day > 0:
        status = "ENTRY_WARMUP_BLOCKED"
        summary = f"Entry warmup blocked {int(warmup_blocked_votes_day)} vote(s); deterministic policy was not reached."
    else:
        status = "NO_POLICY_EVALUATION_TODAY"
        summary = "Directional entries existed but no deterministic policy decisions were persisted."

    return {
        "status": status,
        "summary": summary,
        "counts": {
            "directional_entry_votes_day": int(directional_entry_votes_day),
            "policy_evaluated_votes_day": int(policy_evaluated_votes_day),
            "policy_allowed_votes_day": int(policy_allowed_votes_day),
            "policy_blocked_votes_day": int(policy_blocked_votes_day),
            "warmup_blocked_votes_day": int(warmup_blocked_votes_day),
            "sampled_votes": len(recent_docs),
            "sampled_policy_rows": len(policy_rows),
        },
        "ratios": {
            "policy_pass_rate_day": policy_pass_rate_day,
            "policy_block_rate_day": policy_block_rate_day,
            "warmup_block_rate_day": warmup_block_rate_day,
        },
        "latest_policy_decision": latest_policy,
        "recent_policy_decisions": policy_rows[:20],
    }


__all__ = [
    "policy_row_from_vote_doc",
    "build_deterministic_diagnostics",
]
