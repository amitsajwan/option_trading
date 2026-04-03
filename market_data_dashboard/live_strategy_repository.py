from __future__ import annotations

from typing import Any

from contracts_app.strategy_decision_contract import (
    normalize_decision_mode,
    normalize_engine_mode,
)

try:
    from .strategy_evaluation_service import (
        _iso_or_none,
        _parse_reason,
        _resolve_position_signal_id,
        _safe_float,
    )
except ImportError:
    from strategy_evaluation_service import (  # type: ignore
        _iso_or_none,
        _parse_reason,
        _resolve_position_signal_id,
        _safe_float,
    )


def _coerce_bool(raw: Any) -> bool | None:
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


class LiveStrategyRepository:
    def __init__(
        self,
        evaluation_service: Any,
        *,
        dataset: str = "live",
        snapshot_collection_env: str = "MONGO_COLL_SNAPSHOTS",
        default_snapshot_collection: str = "phase1_market_snapshots",
    ) -> None:
        self._evaluation_service = evaluation_service
        self._dataset = str(dataset or "live").strip().lower() or "live"
        self._snapshot_collection_env = str(snapshot_collection_env or "MONGO_COLL_SNAPSHOTS").strip() or "MONGO_COLL_SNAPSHOTS"
        self._default_snapshot_collection = (
            str(default_snapshot_collection or "phase1_market_snapshots").strip() or "phase1_market_snapshots"
        )

    def collections(self) -> dict[str, Any]:
        names = self._evaluation_service._collection_names(self._dataset)
        db = self._evaluation_service._db()
        return {
            "votes": db[names["votes"]],
            "signals": db[names["signals"]],
            "positions": db[names["positions"]],
            "traces": db[names["traces"]] if "traces" in names else None,
        }

    def snapshot_collection(self) -> Any:
        import os

        coll_name = str(os.getenv(self._snapshot_collection_env) or self._default_snapshot_collection).strip()
        coll_name = coll_name or self._default_snapshot_collection
        return self._evaluation_service._db()[coll_name]

    def load_recent_votes(self, date_ist: str, limit: int) -> list[dict[str, Any]]:
        coll = self.collections()["votes"]
        query = {"trade_date_ist": str(date_ist)}
        projection = {
            "_id": 0,
            "timestamp": 1,
            "strategy": 1,
            "regime": 1,
            "direction": 1,
            "confidence": 1,
            "reason": 1,
            "signal_type": 1,
            "engine_mode": 1,
            "decision_mode": 1,
            "decision_reason_code": 1,
            "decision_metrics": 1,
            "strategy_family_version": 1,
            "strategy_profile_id": 1,
            "payload.vote": 1,
        }
        rows: list[dict[str, Any]] = []
        for doc in coll.find(query, projection).sort("timestamp", -1).limit(int(limit)):
            vote = ((doc.get("payload") or {}).get("vote")) if isinstance(doc.get("payload"), dict) else {}
            vote = vote if isinstance(vote, dict) else {}
            raw_signals = vote.get("raw_signals") if isinstance(vote.get("raw_signals"), dict) else {}
            raw_signals = raw_signals or {}
            rows.append(
                {
                    "timestamp": _iso_or_none(doc.get("timestamp") or vote.get("timestamp")),
                    "strategy": str(doc.get("strategy") or vote.get("strategy") or "").strip() or None,
                    "regime": str(doc.get("regime") or vote.get("regime") or "").strip() or None,
                    "direction": str(doc.get("direction") or vote.get("direction") or "").strip() or None,
                    "confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else vote.get("confidence")),
                    "reason": str(doc.get("reason") or vote.get("reason") or "").strip() or None,
                    "signal_type": str(doc.get("signal_type") or vote.get("signal_type") or "").strip() or None,
                    "proposed_strike": vote.get("proposed_strike"),
                    "proposed_entry_premium": _safe_float(vote.get("proposed_entry_premium")),
                    "policy_allowed": _coerce_bool(raw_signals.get("_policy_allowed")),
                    "policy_score": _safe_float(raw_signals.get("_policy_score")),
                    "policy_reason": str(raw_signals.get("_policy_reason") or "").strip() or None,
                    "policy_checks": raw_signals.get("_policy_checks") if isinstance(raw_signals.get("_policy_checks"), dict) else {},
                    "entry_warmup_blocked": bool(raw_signals.get("_entry_warmup_blocked")),
                    "entry_warmup_reason": str(raw_signals.get("_entry_warmup_reason") or "").strip() or None,
                    "snapshot_id": str(vote.get("snapshot_id") or "").strip() or None,
                    "engine_mode": normalize_engine_mode(doc.get("engine_mode") or vote.get("engine_mode")),
                    "decision_mode": normalize_decision_mode(doc.get("decision_mode") or vote.get("decision_mode")),
                    "decision_reason_code": str(doc.get("decision_reason_code") or vote.get("decision_reason_code") or "").strip() or None,
                    "decision_metrics": (
                        (doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else None)
                        or (vote.get("decision_metrics") if isinstance(vote.get("decision_metrics"), dict) else {})
                    ),
                    "strategy_family_version": str(doc.get("strategy_family_version") or vote.get("strategy_family_version") or "").strip() or None,
                    "strategy_profile_id": str(doc.get("strategy_profile_id") or vote.get("strategy_profile_id") or "").strip() or None,
                }
            )
        return rows

    def load_recent_signals(self, date_ist: str, limit: int) -> list[dict[str, Any]]:
        coll = self.collections()["signals"]
        query = {"trade_date_ist": str(date_ist)}
        projection = {
            "_id": 0,
            "signal_id": 1,
            "signal_type": 1,
            "timestamp": 1,
            "reason": 1,
            "exit_reason": 1,
            "direction": 1,
            "confidence": 1,
            "engine_mode": 1,
            "decision_mode": 1,
            "decision_reason_code": 1,
            "decision_metrics": 1,
            "strategy_family_version": 1,
            "strategy_profile_id": 1,
            "payload.signal": 1,
        }
        rows: list[dict[str, Any]] = []
        for doc in coll.find(query, projection).sort("timestamp", -1).limit(int(limit)):
            signal = ((doc.get("payload") or {}).get("signal")) if isinstance(doc.get("payload"), dict) else {}
            signal = signal if isinstance(signal, dict) else {}
            contributing = signal.get("contributing_strategies") if isinstance(signal.get("contributing_strategies"), list) else []
            strategy = str(signal.get("entry_strategy_name") or "").strip() or None
            if strategy is None and contributing:
                strategy = str(contributing[0] or "").strip() or None
            if strategy is None:
                strategy, _ = _parse_reason(str(doc.get("reason") or signal.get("reason") or ""))
            rows.append(
                {
                    "signal_id": str(doc.get("signal_id") or signal.get("signal_id") or "").strip() or None,
                    "timestamp": _iso_or_none(doc.get("timestamp") or signal.get("timestamp")),
                    "signal_type": str(doc.get("signal_type") or signal.get("signal_type") or "").strip() or None,
                    "direction": str(signal.get("direction") or "").strip() or None,
                    "strategy": strategy,
                    "regime": str(signal.get("regime") or "").strip() or None,
                    "strike": signal.get("strike"),
                    "premium": _safe_float(signal.get("entry_premium")),
                    "position_id": str(signal.get("position_id") or "").strip() or None,
                    "acted_on": _coerce_bool(signal.get("acted_on")),
                    "reason": str(doc.get("reason") or signal.get("reason") or "").strip() or None,
                    "exit_reason": str(doc.get("exit_reason") or signal.get("exit_reason") or "").strip() or None,
                    "snapshot_id": str(signal.get("snapshot_id") or "").strip() or None,
                    "engine_mode": normalize_engine_mode(doc.get("engine_mode") or signal.get("engine_mode")),
                    "decision_mode": normalize_decision_mode(doc.get("decision_mode") or signal.get("decision_mode")),
                    "decision_reason_code": str(doc.get("decision_reason_code") or signal.get("decision_reason_code") or "").strip() or None,
                    "decision_metrics": (
                        (doc.get("decision_metrics") if isinstance(doc.get("decision_metrics"), dict) else None)
                        or (signal.get("decision_metrics") if isinstance(signal.get("decision_metrics"), dict) else {})
                    ),
                    "strategy_family_version": str(doc.get("strategy_family_version") or signal.get("strategy_family_version") or "").strip() or None,
                    "strategy_profile_id": str(doc.get("strategy_profile_id") or signal.get("strategy_profile_id") or "").strip() or None,
                    "confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else signal.get("confidence")),
                }
            )
        return rows

    def load_position_map(self, date_ist: str) -> dict[str, dict[str, Any]]:
        coll = self.collections()["positions"]
        query = {"trade_date_ist": str(date_ist)}
        projection = {
            "_id": 0,
            "position_id": 1,
            "signal_id": 1,
            "event": 1,
            "timestamp": 1,
            "trade_date_ist": 1,
            "payload.position": 1,
        }
        out: dict[str, dict[str, Any]] = {}
        for doc in coll.find(query, projection).sort("timestamp", 1):
            position_id = str(doc.get("position_id") or "").strip()
            if not position_id:
                continue
            payload_position = ((doc.get("payload") or {}).get("position")) if isinstance(doc.get("payload"), dict) else {}
            payload_position = payload_position if isinstance(payload_position, dict) else {}
            if not str(payload_position.get("signal_id") or "").strip():
                payload_position = dict(payload_position)
                resolved_signal_id = _resolve_position_signal_id(payload_position, doc)
                if resolved_signal_id:
                    payload_position["signal_id"] = resolved_signal_id
            slot = out.setdefault(position_id, {"position_id": position_id})
            event = str(doc.get("event") or payload_position.get("event") or "").strip().upper()
            if event == "POSITION_OPEN":
                slot["open"] = payload_position
                slot["open_doc"] = doc
            elif event == "POSITION_CLOSE":
                slot["close"] = payload_position
                slot["close_doc"] = doc
            elif event == "POSITION_MANAGE":
                slot["latest_manage"] = payload_position
                slot["latest_manage_doc"] = doc
        return out

    def latest_trade_date(self) -> str | None:
        coll_map = self.collections()
        for key in ("positions", "signals", "votes", "traces"):
            coll = coll_map[key]
            if coll is None:
                continue
            doc = coll.find_one(
                {"trade_date_ist": {"$exists": True, "$ne": ""}},
                {"_id": 0, "trade_date_ist": 1, "timestamp": 1},
                sort=[("timestamp", -1)],
            )
            if isinstance(doc, dict):
                value = str(doc.get("trade_date_ist") or "").strip()
                if value:
                    return value
        return None

    def snapshot_has_data(self, date_ist: str, instrument: str) -> bool:
        symbol = str(instrument or "").strip()
        if not symbol:
            return False
        coll = self.snapshot_collection()
        return bool(coll.count_documents({"trade_date_ist": str(date_ist), "instrument": symbol}, limit=1))

    def latest_snapshot_instrument(self, date_ist: str) -> str | None:
        coll = self.snapshot_collection()
        doc = coll.find_one(
            {
                "trade_date_ist": str(date_ist),
                "instrument": {"$exists": True, "$nin": ["", None]},
            },
            {"_id": 0, "instrument": 1, "timestamp": 1},
            sort=[("timestamp", -1)],
        )
        if isinstance(doc, dict):
            value = str(doc.get("instrument") or "").strip()
            if value:
                return value
        return None

    def load_recent_trace_digests(
        self,
        date_ist: str,
        limit: int,
        *,
        outcome: str | None = None,
        engine_mode: str | None = None,
        only_blocked: bool = False,
        snapshot_id: str | None = None,
        position_id: str | None = None,
    ) -> list[dict[str, Any]]:
        coll = self.collections().get("traces")
        if coll is None:
            return []
        query: dict[str, Any] = {"trade_date_ist": str(date_ist)}
        outcome_text = str(outcome or "").strip()
        if outcome_text:
            query["final_outcome"] = outcome_text
        engine_text = str(engine_mode or "").strip().lower()
        if engine_text:
            query["engine_mode"] = engine_text
        if bool(only_blocked):
            query["final_outcome"] = "blocked"
        snapshot_text = str(snapshot_id or "").strip()
        if snapshot_text:
            query["snapshot_id"] = snapshot_text
        position_text = str(position_id or "").strip()
        if position_text:
            query["position_id"] = position_text
        projection = {
            "_id": 0,
            "trace_id": 1,
            "snapshot_id": 1,
            "timestamp": 1,
            "trade_date_ist": 1,
            "run_id": 1,
            "engine_mode": 1,
            "decision_mode": 1,
            "evaluation_type": 1,
            "final_outcome": 1,
            "primary_blocker_gate": 1,
            "selected_candidate_id": 1,
            "selected_strategy_name": 1,
            "selected_direction": 1,
            "position_id": 1,
            "candidate_count": 1,
            "blocked_candidate_count": 1,
            "summary_metrics": 1,
        }
        rows: list[dict[str, Any]] = []
        for doc in coll.find(query, projection).sort("timestamp", -1).limit(int(limit)):
            rows.append(
                {
                    "trace_id": str(doc.get("trace_id") or "").strip() or None,
                    "snapshot_id": str(doc.get("snapshot_id") or "").strip() or None,
                    "timestamp": _iso_or_none(doc.get("timestamp")),
                    "trade_date_ist": str(doc.get("trade_date_ist") or "").strip() or None,
                    "run_id": str(doc.get("run_id") or "").strip() or None,
                    "engine_mode": normalize_engine_mode(doc.get("engine_mode")),
                    "decision_mode": normalize_decision_mode(doc.get("decision_mode")),
                    "evaluation_type": str(doc.get("evaluation_type") or "").strip() or None,
                    "final_outcome": str(doc.get("final_outcome") or "").strip() or None,
                    "primary_blocker_gate": str(doc.get("primary_blocker_gate") or "").strip() or None,
                    "selected_candidate_id": str(doc.get("selected_candidate_id") or "").strip() or None,
                    "selected_strategy_name": str(doc.get("selected_strategy_name") or "").strip() or None,
                    "selected_direction": str(doc.get("selected_direction") or "").strip() or None,
                    "position_id": str(doc.get("position_id") or "").strip() or None,
                    "candidate_count": int(doc.get("candidate_count") or 0),
                    "blocked_candidate_count": int(doc.get("blocked_candidate_count") or 0),
                    "summary_metrics": (
                        doc.get("summary_metrics") if isinstance(doc.get("summary_metrics"), dict) else {}
                    ),
                }
            )
        return rows

    def load_trace_detail(self, trace_id: str) -> dict[str, Any] | None:
        coll = self.collections().get("traces")
        trace_text = str(trace_id or "").strip()
        if coll is None or not trace_text:
            return None
        doc = coll.find_one({"trace_id": trace_text}, {"_id": 0, "payload.trace": 1})
        payload = ((doc or {}).get("payload") or {}) if isinstance(doc, dict) else {}
        trace = payload.get("trace") if isinstance(payload, dict) else None
        return trace if isinstance(trace, dict) else None


__all__ = [
    "LiveStrategyRepository",
]
