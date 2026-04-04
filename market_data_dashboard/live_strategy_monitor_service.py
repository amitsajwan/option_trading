from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from contracts_app.market_session import IST_ZONE, is_market_open_ist, load_holidays
from contracts_app.strategy_decision_contract import (
    normalize_decision_mode as _contract_normalize_decision_mode,
    normalize_engine_mode as _contract_normalize_engine_mode,
    parse_metric_token,
)

try:
    from .strategy_evaluation_service import (
        StrategyEvaluationService,
        _iso_or_none,
        _parse_iso_dt,
        _parse_reason,
        _resolve_position_signal_id,
        _safe_float,
    )
except ImportError:
    from strategy_evaluation_service import (  # type: ignore
        StrategyEvaluationService,
        _iso_or_none,
        _parse_iso_dt,
        _parse_reason,
        _resolve_position_signal_id,
        _safe_float,
    )

try:
    from .strategy_monitor_contracts import (
        AlertItem,
        DecisionDiagnostics,
        DecisionExplainability,
        EngineContext,
        LiveStrategySessionPayload,
        OpsState,
        UiHints,
    )
except ImportError:
    from strategy_monitor_contracts import (  # type: ignore
        AlertItem,
        DecisionDiagnostics,
        DecisionExplainability,
        EngineContext,
        LiveStrategySessionPayload,
        OpsState,
        UiHints,
    )

try:
    from .diagnostics.deterministic import (
        build_deterministic_diagnostics as _build_deterministic_diagnostics_module,
        policy_row_from_vote_doc as _policy_row_from_vote_doc_module,
    )
    from .diagnostics.ml_pure import build_ml_pure_diagnostics as _build_ml_pure_diagnostics_module
    from .live_strategy_repository import LiveStrategyRepository
    from .live_strategy_session_assembler import (
        build_session_payload as _build_session_payload_module,
        infer_engine_context as _infer_engine_context_module,
        promotion_lane_from_engine as _promotion_lane_from_engine_module,
    )
    from .ux.alerts import build_active_alerts as _build_active_alerts_module
    from .ux.decision_explainer import build_decision_explainability as _build_decision_explainability_module
except ImportError:
    from diagnostics.deterministic import (  # type: ignore
        build_deterministic_diagnostics as _build_deterministic_diagnostics_module,
        policy_row_from_vote_doc as _policy_row_from_vote_doc_module,
    )
    from diagnostics.ml_pure import build_ml_pure_diagnostics as _build_ml_pure_diagnostics_module  # type: ignore
    from live_strategy_repository import LiveStrategyRepository  # type: ignore
    from live_strategy_session_assembler import (  # type: ignore
        build_session_payload as _build_session_payload_module,
        infer_engine_context as _infer_engine_context_module,
        promotion_lane_from_engine as _promotion_lane_from_engine_module,
    )
    from ux.alerts import build_active_alerts as _build_active_alerts_module  # type: ignore
    from ux.decision_explainer import build_decision_explainability as _build_decision_explainability_module  # type: ignore


def _parse_date_yyyy_mm_dd(raw: Optional[str]) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except Exception as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _safe_limit(raw: Any, *, default: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except Exception as exc:
        raise ValueError("limit must be an integer") from exc
    if value <= 0:
        raise ValueError("limit must be > 0")
    return min(maximum, value)


def _empty_summary_payload(initial_capital: float) -> dict[str, Any]:
    return {
        "overall": {
            "trade_count": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "median_return_pct": None,
        },
        "equity": {
            "start_capital": float(initial_capital),
            "end_capital": float(initial_capital),
            "net_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        },
        "by_strategy": [],
        "by_regime": [],
        "exit_reasons": [],
        "streaks": {"max_win_streak": 0, "max_loss_streak": 0},
        "counts": {
            "signals": 0,
            "positions": 0,
            "trades": 0,
        },
    }


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


def _parse_check_metric(raw: Any, metric_key: str) -> Optional[float]:
    return parse_metric_token(raw, metric_key)


def _safe_ratio(numerator: Any, denominator: Any) -> Optional[float]:
    try:
        num = float(numerator)
        den = float(denominator)
    except Exception:
        return None
    if den <= 0:
        return None
    return num / den


def _stale_open_threshold_seconds() -> int:
    raw = str(os.getenv("LIVE_STRATEGY_STALE_OPEN_SECONDS") or "300").strip()
    try:
        seconds = int(raw)
    except Exception:
        seconds = 300
    return max(60, seconds)


def _ux_v1_enabled() -> bool:
    raw = str(os.getenv("LIVE_STRATEGY_UX_V1", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _decision_trace_enabled() -> bool:
    raw = str(os.getenv("DASHBOARD_ENABLE_DECISION_TRACE", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _normalize_engine_mode(raw: Any) -> Optional[str]:
    return _contract_normalize_engine_mode(raw)


def _normalize_decision_mode(raw: Any) -> Optional[str]:
    return _contract_normalize_decision_mode(raw)


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


def _safe_count_documents(coll: Any, query: dict[str, Any]) -> int:
    counter = getattr(coll, "count_documents", None)
    if not callable(counter):
        return 0
    try:
        return int(counter(query))
    except Exception:
        return 0


class LiveStrategyMonitorService:
    def __init__(
        self,
        evaluation_service: Optional[StrategyEvaluationService] = None,
        *,
        dataset: str = "live",
        snapshot_collection_env: str = "MONGO_COLL_SNAPSHOTS",
        default_snapshot_collection: str = "phase1_market_snapshots",
    ) -> None:
        self._evaluation_service = evaluation_service or StrategyEvaluationService()
        self._dataset = str(dataset or "live").strip().lower() or "live"
        self._snapshot_collection_env = str(snapshot_collection_env or "MONGO_COLL_SNAPSHOTS").strip() or "MONGO_COLL_SNAPSHOTS"
        self._default_snapshot_collection = (
            str(default_snapshot_collection or "phase1_market_snapshots").strip() or "phase1_market_snapshots"
        )
        self._repo = LiveStrategyRepository(
            self._evaluation_service,
            dataset=self._dataset,
            snapshot_collection_env=self._snapshot_collection_env,
            default_snapshot_collection=self._default_snapshot_collection,
        )
        self._holiday_cache: Optional[set[Any]] = None
        self._last_engine_mode: Optional[str] = None

    def get_session_date_ist(self, date_override: Optional[str] = None) -> str:
        parsed = _parse_date_yyyy_mm_dd(date_override)
        if parsed:
            return parsed
        return datetime.now(tz=IST_ZONE).date().isoformat()

    def resolve_session_instrument(self, *, date_ist: str, requested_instrument: Optional[str]) -> Optional[str]:
        instrument_name = str(requested_instrument or "").strip() or None
        if instrument_name and self._repo.snapshot_has_data(date_ist, instrument_name):
            return instrument_name
        fallback = self._repo.latest_snapshot_instrument(date_ist)
        return fallback or instrument_name

    def resolve_live_capital(self, explicit_capital: Optional[float] = None) -> float:
        if explicit_capital is not None:
            value = float(explicit_capital)
            if value <= 0:
                raise ValueError("initial_capital must be > 0")
            return value
        raw = os.getenv("RISK_CAPITAL_ALLOCATED", "500000.0")
        try:
            value = float(raw)
        except Exception:
            value = 500000.0
        return value if value > 0 else 500000.0

    def _policy_row_from_vote_doc(self, doc: dict[str, Any]) -> Optional[dict[str, Any]]:
        return _policy_row_from_vote_doc_module(doc)

    def build_deterministic_diagnostics(
        self,
        *,
        date_ist: str,
        votes_coll: Any,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return _build_deterministic_diagnostics_module(date_ist=date_ist, votes_coll=votes_coll, run_id=run_id)

    def build_ml_pure_diagnostics(self, *, date_ist: str, signals_coll: Any, positions_coll: Any = None) -> dict[str, Any]:
        return _build_ml_pure_diagnostics_module(date_ist=date_ist, signals_coll=signals_coll, positions_coll=positions_coll)

    def build_decision_diagnostics(
        self,
        *,
        date_ist: str,
        votes_coll: Any,
        signals_coll: Any,
        positions_coll: Any = None,
        run_id: Optional[str] = None,
    ) -> DecisionDiagnostics:
        deterministic = self.build_deterministic_diagnostics(date_ist=date_ist, votes_coll=votes_coll, run_id=run_id)
        ml_pure = self.build_ml_pure_diagnostics(date_ist=date_ist, signals_coll=signals_coll, positions_coll=positions_coll)
        return {
            "deterministic": deterministic,
            "ml_pure": ml_pure,
        }

    def load_recent_votes(self, date_ist: str, limit: int, run_id: Optional[str] = None) -> list[dict[str, Any]]:
        return self._repo.load_recent_votes(date_ist, int(limit), run_id)

    def load_recent_signals(self, date_ist: str, limit: int, run_id: Optional[str] = None) -> list[dict[str, Any]]:
        return self._repo.load_recent_signals(date_ist, int(limit), run_id)

    def load_position_map(self, date_ist: str, run_id: Optional[str] = None) -> dict[str, dict[str, Any]]:
        return self._repo.load_position_map(date_ist, run_id)

    def load_recent_trace_digests(
        self,
        date_ist: str,
        limit: int,
        *,
        run_id: Optional[str] = None,
        outcome: Optional[str] = None,
        engine_mode: Optional[str] = None,
        only_blocked: bool = False,
        snapshot_id: Optional[str] = None,
        position_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        loader = getattr(self._repo, "load_recent_trace_digests", None)
        if not callable(loader):
            return []
        return loader(
            date_ist,
            int(limit),
            run_id=run_id,
            outcome=outcome,
            engine_mode=engine_mode,
            only_blocked=only_blocked,
            snapshot_id=snapshot_id,
            position_id=position_id,
        )

    def get_trace_detail(self, trace_id: str) -> Optional[dict[str, Any]]:
        loader = getattr(self._repo, "load_trace_detail", None)
        if not callable(loader):
            return None
        return loader(trace_id)

    def build_decision_trace_summary(self, digests: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [item for item in digests if isinstance(item, dict)]
        blocked = sum(1 for item in rows if str(item.get("final_outcome") or "").strip().lower() == "blocked")
        entries = sum(1 for item in rows if str(item.get("final_outcome") or "").strip().lower() == "entry_taken")
        exits = sum(1 for item in rows if str(item.get("final_outcome") or "").strip().lower() == "exit_taken")
        blockers: dict[str, int] = {}
        for item in rows:
            gate = str(item.get("primary_blocker_gate") or "").strip()
            if not gate:
                continue
            blockers[gate] = int(blockers.get(gate, 0) + 1)
        top_blockers = [
            {"gate": key, "count": value}
            for key, value in sorted(blockers.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        latest = rows[0] if rows else None
        return {
            "sampled_traces": len(rows),
            "blocked_traces": blocked,
            "entry_traces": entries,
            "exit_traces": exits,
            "top_blockers": top_blockers,
            "latest_outcome": str((latest or {}).get("final_outcome") or "").strip() or None,
        }

    def build_current_open_positions(
        self,
        position_map: dict[str, dict[str, Any]],
        signal_map: dict[str, dict[str, Any]],
        *,
        initial_capital: float,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for position_id, docs in position_map.items():
            open_position = docs.get("open")
            close_position = docs.get("close")
            if not isinstance(open_position, dict) or isinstance(close_position, dict):
                continue
            latest_manage = docs.get("latest_manage") if isinstance(docs.get("latest_manage"), dict) else {}
            open_doc = docs.get("open_doc") if isinstance(docs.get("open_doc"), dict) else {}
            latest_manage_doc = docs.get("latest_manage_doc") if isinstance(docs.get("latest_manage_doc"), dict) else {}
            signal_id = str(
                _resolve_position_signal_id(
                    open_position,
                    open_doc,
                    latest_manage,
                    latest_manage_doc,
                )
                or ""
            ).strip()
            signal_doc = signal_map.get(signal_id, {})
            reason_text = str(signal_doc.get("reason") or open_position.get("reason") or "")
            strategy_from_reason, regime_from_reason = _parse_reason(reason_text)
            current_premium = _safe_float(latest_manage.get("current_premium"))
            pnl_pct = _safe_float(latest_manage.get("pnl_pct"))
            capital_at_risk = None
            unrealized_amount = None
            unrealized_pct = None
            entry_premium = _safe_float(open_position.get("entry_premium"))
            lots = _safe_float(open_position.get("lots"))
            lot_size = 15.0
            if entry_premium is not None and lots is not None and entry_premium > 0 and lots > 0:
                capital_at_risk = entry_premium * lots * lot_size
            if capital_at_risk is not None and pnl_pct is not None:
                unrealized_amount = capital_at_risk * pnl_pct
                if initial_capital > 0:
                    unrealized_pct = unrealized_amount / initial_capital
            rows.append(
                {
                    "position_id": position_id,
                    "signal_id": signal_id or None,
                    "strategy": str(open_position.get("entry_strategy") or strategy_from_reason or "").strip() or None,
                    "regime": str(open_position.get("entry_regime") or signal_doc.get("regime") or regime_from_reason or "").strip() or None,
                    "direction": str(open_position.get("direction") or "").strip() or None,
                    "strike": open_position.get("strike"),
                    "entry_time": _iso_or_none((docs.get("open_doc") or {}).get("timestamp") or open_position.get("timestamp")),
                    "entry_premium": entry_premium,
                    "current_time": _iso_or_none((docs.get("latest_manage_doc") or {}).get("timestamp") or latest_manage.get("timestamp")),
                    "current_premium": current_premium,
                    "pnl_pct": pnl_pct,
                    "capital_at_risk": capital_at_risk,
                    "unrealized_pnl_amount": unrealized_amount,
                    "unrealized_pnl_pct": unrealized_pct,
                    "bars_held": latest_manage.get("bars_held"),
                    "stop_price": _safe_float(latest_manage.get("stop_price") if latest_manage else open_position.get("stop_price")),
                    "high_water_premium": _safe_float(latest_manage.get("high_water_premium") if latest_manage else open_position.get("high_water_premium")),
                    "target_pct": _safe_float(open_position.get("target_pct")),
                    "stop_loss_pct": _safe_float(open_position.get("stop_loss_pct")),
                    "trailing_enabled": _coerce_bool(open_position.get("trailing_enabled")),
                    "trailing_active": _coerce_bool(latest_manage.get("trailing_active") if latest_manage else open_position.get("trailing_active")),
                    "entry_reason": str(open_position.get("reason") or signal_doc.get("reason") or "").strip() or None,
                }
            )
        rows.sort(key=lambda item: (_parse_iso_dt(item.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return rows

    def build_latest_closed_trade(
        self,
        position_map: dict[str, dict[str, Any]],
        signal_map: dict[str, dict[str, Any]],
        *,
        initial_capital: float,
    ) -> Optional[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        for position_id, docs in position_map.items():
            trade = self._evaluation_service._trade_from_docs(
                position_id=position_id,
                docs=docs,
                signal_map=signal_map,
                cost_bps=0.0,
            )
            if trade is not None:
                trades.append(trade)
        if not trades:
            return None
        enriched = self._evaluation_service._apply_capital_metrics(trades, initial_capital=float(initial_capital))
        enriched.sort(
            key=lambda item: (
                _parse_iso_dt(item.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("position_id") or ""),
            ),
            reverse=True,
        )
        return enriched[0]

    def build_recent_activity(
        self,
        *,
        current_open_positions: list[dict[str, Any]],
        recent_closed_trades: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for pos in current_open_positions:
            rows.append(
                {
                    "status": "OPEN",
                    "event_time": pos.get("current_time") or pos.get("entry_time"),
                    "entry_time": pos.get("entry_time"),
                    "last_update_time": pos.get("current_time") or pos.get("entry_time"),
                    "position_id": pos.get("position_id"),
                    "strategy": pos.get("strategy"),
                    "direction": pos.get("direction"),
                    "strike": pos.get("strike"),
                    "entry_premium": _safe_float(pos.get("entry_premium")),
                    "current_premium": _safe_float(pos.get("current_premium")),
                    "exit_reason": None,
                    "capital_pnl_amount": _safe_float(pos.get("unrealized_pnl_amount")),
                    "capital_pnl_pct": _safe_float(pos.get("unrealized_pnl_pct")),
                }
            )
        for trade in recent_closed_trades:
            rows.append(
                {
                    "status": "CLOSED",
                    "event_time": trade.get("exit_time") or trade.get("entry_time"),
                    "entry_time": trade.get("entry_time"),
                    "last_update_time": trade.get("exit_time") or trade.get("entry_time"),
                    "position_id": trade.get("position_id"),
                    "strategy": trade.get("entry_strategy"),
                    "direction": trade.get("direction"),
                    "strike": trade.get("strike"),
                    "entry_premium": _safe_float(trade.get("entry_premium")),
                    "current_premium": _safe_float(trade.get("exit_premium")),
                    "exit_reason": trade.get("exit_reason"),
                    "capital_pnl_amount": _safe_float(trade.get("capital_pnl_amount")),
                    "capital_pnl_pct": _safe_float(trade.get("capital_pnl_pct")),
                }
            )
        rows.sort(
            key=lambda item: (_parse_iso_dt(item.get("event_time")) or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return rows[: max(1, int(limit))]

    def build_chart_markers(
        self,
        closed_trades: list[dict[str, Any]],
        current_open_positions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        for trade in closed_trades:
            direction = str(trade.get("direction") or "").strip() or None
            strike = trade.get("strike")
            strategy = str(trade.get("entry_strategy") or "").strip() or None
            regime = str(trade.get("regime") or "").strip() or None
            entry_label = " ".join([part for part in [strategy, direction, str(strike) if strike is not None else ""] if part]).strip()
            markers.append(
                {
                    "position_id": str(trade.get("position_id") or "").strip() or None,
                    "type": "entry",
                    "timestamp": trade.get("entry_time"),
                    "label": entry_label or "ENTRY",
                    "strategy": strategy,
                    "regime": regime,
                    "direction": direction,
                    "strike": strike,
                    "premium": _safe_float(trade.get("entry_premium")),
                    "result": None,
                }
            )
            pnl_pct_net = _safe_float(trade.get("pnl_pct_net"))
            pnl_text = f"{pnl_pct_net * 100.0:+.2f}%" if pnl_pct_net is not None else ""
            exit_label = " ".join(
                [part for part in [str(trade.get("exit_reason") or "").strip() or "EXIT", pnl_text] if part]
            ).strip()
            markers.append(
                {
                    "position_id": str(trade.get("position_id") or "").strip() or None,
                    "type": "exit",
                    "timestamp": trade.get("exit_time"),
                    "label": exit_label,
                    "strategy": strategy,
                    "regime": regime,
                    "direction": direction,
                    "strike": strike,
                    "premium": _safe_float(trade.get("exit_premium")),
                    "result": str(trade.get("result") or "").strip() or None,
                }
            )
        for position in current_open_positions:
            direction = str(position.get("direction") or "").strip() or None
            strike = position.get("strike")
            strategy = str(position.get("strategy") or "").strip() or None
            markers.append(
                {
                    "position_id": str(position.get("position_id") or "").strip() or None,
                    "type": "entry",
                    "timestamp": position.get("entry_time"),
                    "label": " ".join(
                        [part for part in [strategy, direction, str(strike) if strike is not None else ""] if part]
                    ).strip()
                    or "ENTRY",
                    "strategy": strategy,
                    "regime": str(position.get("regime") or "").strip() or None,
                    "direction": direction,
                    "strike": strike,
                    "premium": _safe_float(position.get("entry_premium")),
                    "result": None,
                }
            )
        markers.sort(
            key=lambda item: (
                _parse_iso_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
                0 if str(item.get("type") or "") == "entry" else 1,
            )
        )
        return markers

    def build_freshness(
        self,
        latest_vote_ts: Optional[str],
        latest_signal_ts: Optional[str],
        latest_position_ts: Optional[str],
    ) -> dict[str, Any]:
        max_age = int(os.getenv("LIVE_STRATEGY_FRESHNESS_SECONDS") or "180")
        now_utc = datetime.now(tz=timezone.utc)

        def as_age(raw: Optional[str]) -> Optional[int]:
            parsed = _parse_iso_dt(raw)
            if parsed is None:
                return None
            return max(0, int((now_utc - parsed).total_seconds()))

        vote_age = as_age(latest_vote_ts)
        signal_age = as_age(latest_signal_ts)
        position_age = as_age(latest_position_ts)
        return {
            "votes_fresh": vote_age is not None and vote_age <= max_age,
            "signals_fresh": signal_age is not None and signal_age <= max_age,
            "positions_fresh": position_age is not None and position_age <= max_age,
            "latest_vote_age_sec": vote_age,
            "latest_signal_age_sec": signal_age,
            "latest_position_age_sec": position_age,
        }

    def partition_open_positions(
        self,
        *,
        current_open_positions: list[dict[str, Any]],
        latest_position_ts: Optional[str],
        market_session_open: bool,
        reference_time_utc: Optional[datetime] = None,
        stale_after_seconds: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now_utc = reference_time_utc or datetime.now(tz=timezone.utc)
        stale_threshold_seconds = int(stale_after_seconds or _stale_open_threshold_seconds())
        latest_position_dt = _parse_iso_dt(latest_position_ts) if latest_position_ts else None
        active: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []

        for row in current_open_positions:
            last_update_raw = row.get("current_time") or row.get("entry_time")
            last_update_dt = _parse_iso_dt(last_update_raw)
            stale_reason: Optional[str] = None
            stale_age_sec: Optional[int] = None
            lag_vs_latest_position_sec: Optional[int] = None

            if last_update_dt is not None:
                stale_age_sec = max(0, int((now_utc - last_update_dt).total_seconds()))
            if latest_position_dt is not None and last_update_dt is not None:
                lag_vs_latest_position_sec = max(0, int((latest_position_dt - last_update_dt).total_seconds()))

            if market_session_open:
                if last_update_dt is None:
                    stale_reason = "missing_last_update"
                elif latest_position_dt is not None and lag_vs_latest_position_sec is not None and lag_vs_latest_position_sec > stale_threshold_seconds:
                    stale_reason = "lag_vs_latest_position"
                elif latest_position_dt is None and stale_age_sec is not None and stale_age_sec > stale_threshold_seconds:
                    stale_reason = "age_exceeds_threshold"

            if stale_reason is None:
                active.append(row)
                continue

            stale_row = dict(row)
            stale_row["stale_reason"] = stale_reason
            stale_row["stale_age_sec"] = stale_age_sec
            stale_row["lag_vs_latest_position_sec"] = lag_vs_latest_position_sec
            stale_row["reconciliation_action"] = "exclude_from_active_session"
            stale.append(stale_row)

        return active, stale

    def infer_engine_context(
        self,
        *,
        recent_votes: list[dict[str, Any]],
        recent_signals: list[dict[str, Any]],
    ) -> EngineContext:
        return _infer_engine_context_module(
            recent_votes=recent_votes,
            recent_signals=recent_signals,
        )

    @staticmethod
    def promotion_lane_from_engine(active_engine_mode: Optional[str]) -> str:
        return _promotion_lane_from_engine_module(active_engine_mode)

    def build_ops_state(
        self,
        *,
        market_session_open: bool,
        engine_context: EngineContext,
        freshness: dict[str, Any],
        latest_decision: Optional[dict[str, Any]],
        active_alerts: list[AlertItem],
    ) -> OpsState:
        market_state = "open" if bool(market_session_open) else "closed"
        mode = str(engine_context.get("active_engine_mode") or "unknown").strip().lower()
        if mode == "ml_pure":
            engine_state = "ml_pure_active"
        elif mode == "deterministic":
            engine_state = "deterministic_active"
        else:
            engine_state = "unknown"

        reason_code = str((latest_decision or {}).get("reason_code") or "").strip().lower()
        risk_state = "normal"
        if reason_code == "risk_halt":
            risk_state = "halted"
        elif reason_code == "risk_pause":
            risk_state = "paused"

        votes_fresh = bool(freshness.get("votes_fresh"))
        signals_fresh = bool(freshness.get("signals_fresh"))
        positions_fresh = bool(freshness.get("positions_fresh"))
        all_fresh = votes_fresh and signals_fresh and positions_fresh
        has_critical = any(str(alert.get("severity") or "").lower() == "critical" for alert in active_alerts)
        if all_fresh and not has_critical:
            data_health_state = "ok"
        elif has_critical:
            data_health_state = "critical"
        else:
            data_health_state = "warn"

        active_blocker = reason_code or None
        if active_blocker is None and active_alerts:
            active_blocker = str(active_alerts[0].get("id") or "").strip() or None

        return {
            "market_state": market_state,
            "engine_state": engine_state,
            "risk_state": risk_state,
            "data_health_state": data_health_state,
            "active_blocker": active_blocker,
        }

    def build_ui_hints(
        self,
        *,
        engine_context: EngineContext,
        active_alerts: list[AlertItem],
        freshness: dict[str, Any],
        debug_view: bool,
    ) -> UiHints:
        mode = str(engine_context.get("active_engine_mode") or "").strip().lower()
        panel = "ml_pure" if mode == "ml_pure" else "deterministic"
        has_critical = any(str(alert.get("severity") or "").lower() == "critical" for alert in active_alerts)
        has_warning = any(str(alert.get("severity") or "").lower() == "warning" for alert in active_alerts)
        all_fresh = bool(freshness.get("votes_fresh")) and bool(freshness.get("signals_fresh")) and bool(freshness.get("positions_fresh"))
        if has_critical or has_warning:
            recommended_focus_panel = "active_alerts"
        elif panel == "ml_pure":
            recommended_focus_panel = "decision_timeline"
        else:
            recommended_focus_panel = "engine_panel"
        return {
            "active_engine_panel": panel,
            "recommended_focus_panel": recommended_focus_panel,
            "degraded_mode": (not all_fresh) or bool(has_critical),
            "debug_view": bool(debug_view),
        }

    def get_live_strategy_session(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.get_strategy_session(**kwargs)

    def get_strategy_session(
        self,
        *,
        date: Optional[str] = None,
        instrument: Optional[str] = None,
        run_id: Optional[str] = None,
        limit_votes: Any = None,
        limit_signals: Any = None,
        limit_trades: Any = None,
        initial_capital: Optional[float] = None,
        timeline_limit: Any = None,
        debug_view: Any = None,
    ) -> dict[str, Any]:
        date_ist = self.get_session_date_ist(date)
        requested_instrument = str(instrument or os.getenv("INSTRUMENT_SYMBOL") or "").strip() or None
        instrument_name = self.resolve_session_instrument(date_ist=date_ist, requested_instrument=requested_instrument)
        vote_limit = _safe_limit(limit_votes, default=25, maximum=100)
        signal_limit = _safe_limit(limit_signals, default=25, maximum=100)
        trade_limit = _safe_limit(limit_trades, default=20, maximum=100)
        resolved_timeline_limit = _safe_limit(timeline_limit, default=25, maximum=100)
        raw_debug_view = _coerce_bool(debug_view)
        resolved_debug_view = bool(raw_debug_view) if raw_debug_view is not None else False
        resolved_capital = self.resolve_live_capital(initial_capital)
        resolved_run_id = str(run_id or "").strip() or None

        coll_map = self._repo.collections()
        votes_coll = coll_map["votes"]
        signals_coll = coll_map["signals"]
        positions_coll = coll_map["positions"]
        date_match = {"trade_date_ist": str(date_ist)}
        if resolved_run_id:
            date_match["run_id"] = resolved_run_id
        signal_map = self._evaluation_service._load_signal_map(
            signals_coll=signals_coll,
            date_match=date_match,
        )
        position_map = self.load_position_map(date_ist, resolved_run_id)
        recent_votes = self.load_recent_votes(date_ist, vote_limit, resolved_run_id)
        recent_signals = self.load_recent_signals(date_ist, signal_limit, resolved_run_id)
        recent_trace_digests: list[dict[str, Any]] = []
        if _decision_trace_enabled():
            recent_trace_digests = self.load_recent_trace_digests(
                date_ist,
                resolved_timeline_limit,
                run_id=resolved_run_id,
            )
        decision_diagnostics = self.build_decision_diagnostics(
            date_ist=date_ist,
            votes_coll=votes_coll,
            signals_coll=signals_coll,
            positions_coll=positions_coll,
            run_id=resolved_run_id,
        )
        engine_context = self.infer_engine_context(
            recent_votes=recent_votes,
            recent_signals=recent_signals,
        )
        promotion_lane = self.promotion_lane_from_engine(engine_context.get("active_engine_mode"))
        current_positions = self.build_current_open_positions(
            position_map,
            signal_map,
            initial_capital=resolved_capital,
        )
        latest_closed_trade = self.build_latest_closed_trade(
            position_map,
            signal_map,
            initial_capital=resolved_capital,
        )

        try:
            trades_payload = self._evaluation_service.compute_trades(
                dataset=self._dataset,
                date_from=date_ist,
                date_to=date_ist,
                strategies=[],
                regimes=[],
                initial_capital=resolved_capital,
                cost_bps=0.0,
                page=1,
                page_size=trade_limit,
                sort_by="exit_time",
                sort_dir="desc",
                run_id=resolved_run_id,
            )
            recent_trades = list(trades_payload.get("rows") or [])
        except ValueError as exc:
            if self._dataset == "historical" and "no completed historical evaluation runs found" in str(exc):
                trades_payload = {"rows": []}
                recent_trades = []
            else:
                raise
        if latest_closed_trade is None and recent_trades:
            latest_closed_trade = recent_trades[0]

        try:
            summary = self._evaluation_service.compute_summary(
                dataset=self._dataset,
                date_from=date_ist,
                date_to=date_ist,
                strategies=[],
                regimes=[],
                initial_capital=resolved_capital,
                cost_bps=0.0,
                run_id=resolved_run_id,
            )
        except ValueError as exc:
            if self._dataset == "historical" and "no completed historical evaluation runs found" in str(exc):
                summary = _empty_summary_payload(resolved_capital)
            else:
                raise

        latest_vote_ts = recent_votes[0]["timestamp"] if recent_votes else None
        latest_signal_ts = recent_signals[0]["timestamp"] if recent_signals else None
        latest_position_ts = None
        for docs in position_map.values():
            for key in ("close_doc", "latest_manage_doc", "open_doc"):
                ts = _iso_or_none((docs.get(key) or {}).get("timestamp")) if isinstance(docs.get(key), dict) else None
                if ts and (latest_position_ts is None or (_parse_iso_dt(ts) or datetime.min.replace(tzinfo=timezone.utc)) > (_parse_iso_dt(latest_position_ts) or datetime.min.replace(tzinfo=timezone.utc))):
                    latest_position_ts = ts
        market_session_open = self._is_market_session_open()
        active_positions, stale_positions = self.partition_open_positions(
            current_open_positions=current_positions,
            latest_position_ts=latest_position_ts,
            market_session_open=market_session_open,
        )
        event_candidates = [value for value in [latest_vote_ts, latest_signal_ts, latest_position_ts] if value]
        latest_event_time = None
        if event_candidates:
            latest_event_time = max(event_candidates, key=lambda item: _parse_iso_dt(item) or datetime.min.replace(tzinfo=timezone.utc))

        warnings: list[str] = []
        if len(active_positions) > 1:
            warnings.append("multiple_open_positions_detected")
        if stale_positions:
            warnings.append("stale_open_positions_detected")

        recent_activity = self.build_recent_activity(
            current_open_positions=active_positions,
            recent_closed_trades=recent_trades,
            limit=trade_limit,
        )

        freshness_payload = self.build_freshness(latest_vote_ts, latest_signal_ts, latest_position_ts)
        raw_signal_count = _safe_count_documents(signals_coll, date_match)
        raw_vote_count = _safe_count_documents(votes_coll, date_match)
        raw_position_count = _safe_count_documents(positions_coll, date_match)
        counts_payload = {
            **dict(summary.get("counts") or {}),
            "open_positions": len(active_positions),
            "stale_open_positions": len(stale_positions),
        }
        if self._dataset == "historical":
            # Historical replays can legitimately populate raw collections before any
            # evaluation summary exists. Prefer the observed collection counts so the
            # session API reflects replay activity instead of summary fallback zeros.
            counts_payload["votes"] = max(int(counts_payload.get("votes") or 0), raw_vote_count)
            counts_payload["signals"] = max(int(counts_payload.get("signals") or 0), raw_signal_count)
            counts_payload["positions"] = max(int(counts_payload.get("positions") or 0), raw_position_count)
            counts_payload["closed_trades"] = max(
                int(counts_payload.get("closed_trades") or 0),
                int(counts_payload.get("trades") or 0),
                len(recent_trades),
            )

        ops_state: Optional[OpsState] = None
        active_alerts: Optional[list[AlertItem]] = None
        decision_explainability: Optional[DecisionExplainability] = None
        ui_hints: Optional[UiHints] = None
        if _ux_v1_enabled():
            decision_explainability = _build_decision_explainability_module(
                recent_signals=recent_signals,
                recent_votes=recent_votes,
                decision_diagnostics=decision_diagnostics,
                timeline_limit=resolved_timeline_limit,
                debug_view=resolved_debug_view,
            )
            latest_decision = (
                decision_explainability.get("latest_decision") if isinstance(decision_explainability, dict) else None
            )
            active_alerts = _build_active_alerts_module(
                freshness=freshness_payload,
                stale_open_positions=stale_positions,
                warnings=warnings,
                engine_context=engine_context,
                decision_diagnostics=decision_diagnostics,
                counts=counts_payload,
                latest_decision=latest_decision if isinstance(latest_decision, dict) else None,
                previous_engine_mode=self._last_engine_mode,
            )
            ops_state = self.build_ops_state(
                market_session_open=market_session_open,
                engine_context=engine_context,
                freshness=freshness_payload,
                latest_decision=latest_decision if isinstance(latest_decision, dict) else None,
                active_alerts=active_alerts,
            )
            ui_hints = self.build_ui_hints(
                engine_context=engine_context,
                active_alerts=active_alerts,
                freshness=freshness_payload,
                debug_view=resolved_debug_view,
            )

        current_engine_mode = _normalize_engine_mode(engine_context.get("active_engine_mode"))
        if current_engine_mode:
            self._last_engine_mode = current_engine_mode

        return _build_session_payload_module(
            session={
                "date_ist": date_ist,
                "instrument": instrument_name,
                "timezone": "Asia/Kolkata",
                "latest_event_time": latest_event_time,
                "market_session_open": market_session_open,
                "data_freshness": freshness_payload,
                "dataset": self._dataset,
            },
            engine_context=engine_context,
            promotion_lane=promotion_lane,
            capital={
                "configured_capital": resolved_capital,
                "realized_pnl_amount": float(summary.get("equity", {}).get("end_capital") or resolved_capital) - resolved_capital,
                "realized_pnl_pct": summary.get("equity", {}).get("net_return_pct"),
            },
            counts=counts_payload,
            warnings=warnings,
            current_position=(active_positions[0] if active_positions else None),
            current_positions=active_positions,
            stale_open_positions=stale_positions,
            reconciliation={
                "stale_open_count": len(stale_positions),
                "stale_open_threshold_seconds": _stale_open_threshold_seconds(),
                "stale_open_positions": stale_positions[:25],
            },
            latest_closed_trade=latest_closed_trade,
            session_chart=self.load_session_underlying_chart(date_ist=date_ist, instrument=instrument_name),
            today_summary={
                "overall": summary.get("overall"),
                "equity": summary.get("equity"),
                "by_strategy": summary.get("by_strategy"),
                "by_regime": summary.get("by_regime"),
                "exit_reasons": summary.get("exit_reasons"),
                "streaks": summary.get("streaks"),
            },
            recent_trades=recent_trades,
            recent_activity=recent_activity,
            recent_signals=recent_signals,
            recent_votes=recent_votes,
            decision_diagnostics=decision_diagnostics,
            ops_state=ops_state,
            active_alerts=active_alerts,
            decision_explainability=decision_explainability,
            ui_hints=ui_hints,
            decision_trace_summary=(self.build_decision_trace_summary(recent_trace_digests) if recent_trace_digests else None),
            latest_trace_digest=(recent_trace_digests[0] if recent_trace_digests else None),
            decision_trace_available=_decision_trace_enabled(),
            chart_markers=self.build_chart_markers(recent_trades, active_positions),
        )

    def load_session_underlying_chart(self, *, date_ist: str, instrument: Optional[str]) -> Optional[dict[str, Any]]:
        coll = self._repo.snapshot_collection()
        projection = {
            "_id": 0,
            "instrument": 1,
            "timestamp": 1,
            "payload.snapshot.session_context.timestamp": 1,
            "payload.snapshot.session_context.time": 1,
            "payload.snapshot.futures_bar.fut_close": 1,
        }
        def _collect(query: dict[str, Any]) -> tuple[list[str], list[str], list[float], Optional[str]]:
            timestamps: list[str] = []
            labels: list[str] = []
            prices: list[float] = []
            resolved_instrument: Optional[str] = None
            for doc in coll.find(query, projection).sort("timestamp", 1):
                payload = (doc.get("payload") or {}) if isinstance(doc.get("payload"), dict) else {}
                snapshot = (payload.get("snapshot") or {}) if isinstance(payload.get("snapshot"), dict) else {}
                session_context = (snapshot.get("session_context") or {}) if isinstance(snapshot.get("session_context"), dict) else {}
                futures_bar = (snapshot.get("futures_bar") or {}) if isinstance(snapshot.get("futures_bar"), dict) else {}
                price = _safe_float(futures_bar.get("fut_close"))
                if price is None:
                    continue
                ts = _iso_or_none(doc.get("timestamp")) or _iso_or_none(session_context.get("timestamp"))
                if ts is None:
                    continue
                if resolved_instrument is None:
                    value = str(doc.get("instrument") or "").strip()
                    resolved_instrument = value or None
                label = str(session_context.get("time") or "").strip()
                if not label:
                    parsed = _parse_iso_dt(session_context.get("timestamp") or doc.get("timestamp"))
                    label = parsed.astimezone(IST_ZONE).strftime("%H:%M") if parsed is not None else str(ts)[11:16]
                timestamps.append(ts)
                labels.append(label)
                prices.append(price)
            return timestamps, labels, prices, resolved_instrument

        query: dict[str, Any] = {"trade_date_ist": str(date_ist)}
        if instrument:
            query["instrument"] = str(instrument)
        timestamps, labels, prices, resolved_instrument = _collect(query)
        source = "mongo_snapshots"
        if not timestamps and instrument:
            timestamps, labels, prices, resolved_instrument = _collect({"trade_date_ist": str(date_ist)})
            source = "mongo_snapshots:fallback_instrument"
        if not timestamps:
            return None
        return {
            "timestamps": timestamps,
            "labels": labels,
            "prices": prices,
            "instrument": resolved_instrument or instrument,
            "source": source,
        }

    def _holidays(self) -> set[Any]:
        if self._holiday_cache is not None:
            return self._holiday_cache
        configured = str(os.getenv("NSE_HOLIDAYS_FILE") or "").strip()
        if configured:
            path = Path(configured)
        else:
            path = Path(__file__).resolve().parent.parent / "config" / "nse_holidays.json"
        self._holiday_cache = load_holidays(path)
        return self._holiday_cache

    def _is_market_session_open(self) -> bool:
        if str(os.getenv("MARKET_SESSION_ENABLED", "1")).strip() in {"0", "false", "False"}:
            return True
        open_time = str(os.getenv("MARKET_OPEN_TIME") or "09:15").strip() or "09:15"
        close_time = str(os.getenv("MARKET_CLOSE_TIME") or "15:30").strip() or "15:30"
        now_ist = datetime.now(tz=IST_ZONE)
        return is_market_open_ist(now_ist, open_time, close_time, self._holidays())
