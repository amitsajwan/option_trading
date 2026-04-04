import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

import market_data_dashboard.app as dashboard_app


class _FakeLiveMonitorService:
    def __init__(self) -> None:
        self.last_kwargs = {}

    def get_live_strategy_session(self, **kwargs: object) -> dict:
        self.last_kwargs = dict(kwargs)
        return {
            "status": "ok",
            "session": {
                "date_ist": "2026-03-02",
                "instrument": "BANKNIFTY26MARFUT",
                "timezone": "Asia/Kolkata",
                "latest_event_time": "2026-03-02T07:17:00Z",
                "market_session_open": True,
                "data_freshness": {
                    "votes_fresh": True,
                    "signals_fresh": True,
                    "positions_fresh": True,
                    "latest_vote_age_sec": 10,
                },
            },
            "capital": {
                "configured_capital": 500000.0,
                "realized_pnl_amount": 270.0,
                "realized_pnl_pct": 0.00054,
            },
            "engine_context": {
                "active_engine_mode": "ml_pure",
                "strategy_family_version": "ML_PURE_STAGED_V1",
                "strategy_profile_id": "ml_pure_staged_v1",
            },
            "promotion_lane": "ml_pure",
            "counts": {
                "votes": 47,
                "signals": 2,
                "position_events": 4,
                "closed_trades": 1,
                "open_positions": 0,
            },
            "warnings": [],
            "current_position": None,
            "current_positions": [],
            "latest_closed_trade": {
                "position_id": "fa27d563",
                "entry_time": "2026-03-02T07:15:00Z",
                "exit_time": "2026-03-02T07:17:00Z",
                "capital_at_risk": 13830.0,
            },
            "today_summary": {
                "overall": {},
                "equity": {},
                "by_strategy": [],
                "by_regime": [],
                "exit_reasons": [],
            },
            "recent_trades": [
                {
                    "position_id": "fa27d563",
                    "entry_time": "2026-03-02T07:15:00Z",
                    "exit_time": "2026-03-02T07:17:00Z",
                    "capital_at_risk": 13830.0,
                }
            ],
            "recent_signals": [],
            "recent_votes": [],
            "decision_diagnostics": {
                "ml_pure": {"status": "ML_PURE_ACTIVE_TODAY"},
                "deterministic": {"status": "POLICY_ACTIVE_TODAY"},
            },
            "ops_state": {
                "market_state": "open",
                "engine_state": "ml_pure_active",
                "risk_state": "normal",
                "data_health_state": "ok",
                "active_blocker": None,
            },
            "active_alerts": [
                {
                    "id": "warmup_blocks_present",
                    "severity": "info",
                    "title": "Warmup Blocks Observed",
                    "detail": "Warmup blocked 1 candidate(s) today.",
                    "first_seen_ist": "2026-03-02T12:40:00+05:30",
                    "last_seen_ist": "2026-03-02T12:47:00+05:30",
                    "occurrences": 1,
                    "source": "decision_diagnostics.deterministic",
                    "operator_next_step": "No action unless warmup extends beyond expected window.",
                }
            ],
            "decision_explainability": {
                "latest_decision": {
                    "id": "signal:s1",
                    "ts": "2026-03-02T07:17:00Z",
                    "engine_mode": "ml_pure",
                    "decision_mode": "ml_staged",
                    "action": "HOLD",
                    "reason_code": "low_edge_conflict",
                    "explanation": "Edge is insufficient.",
                    "operator_hint": "Wait for stronger edge.",
                    "metrics": {"edge": 0.01, "confidence": 0.62},
                    "source_ref": "signal:s1",
                    "gate_path": "Candidate -> ML Staged -> Risk/Phase -> HOLD",
                },
                "timeline": [],
                "gate_funnel": {},
                "reason_playbook_summary": [],
            },
            "ui_hints": {
                "active_engine_panel": "ml_pure",
                "recommended_focus_panel": "decision_timeline",
                "degraded_mode": False,
                "debug_view": False,
            },
            "decision_trace_summary": {
                "sampled_traces": 1,
                "blocked_traces": 1,
                "entry_traces": 0,
                "exit_traces": 0,
                "top_blockers": [{"gate": "policy_checks", "count": 1}],
                "latest_outcome": "blocked",
            },
            "latest_trace_digest": {
                "trace_id": "trace-1",
                "timestamp": "2026-03-02T07:17:00Z",
                "final_outcome": "blocked",
                "primary_blocker_gate": "policy_checks",
                "selected_strategy_name": None,
                "candidate_count": 2,
                "blocked_candidate_count": 2,
                "summary_metrics": {"candidate_count": 2},
            },
            "decision_trace_available": True,
            "chart_markers": [],
        }

    def get_session_date_ist(self, date_override: object = None) -> str:
        return str(date_override or "2026-03-02")

    def load_recent_trace_digests(self, date_ist: str, limit: int, **kwargs: object) -> list[dict]:
        self.last_kwargs = {"date_ist": date_ist, "limit": limit, **dict(kwargs)}
        return [
            {
                "trace_id": "trace-1",
                "snapshot_id": "snap-1",
                "timestamp": "2026-03-02T07:17:00Z",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "evaluation_type": "entry",
                "final_outcome": "blocked",
                "primary_blocker_gate": "policy_checks",
                "selected_strategy_name": None,
                "selected_direction": None,
                "candidate_count": 2,
                "blocked_candidate_count": 2,
                "summary_metrics": {"candidate_count": 2},
            }
        ]

    def get_trace_detail(self, trace_id: str) -> dict:
        return {
            "trace_id": trace_id,
            "timestamp": "2026-03-02T07:17:00Z",
            "engine_mode": "ml_pure",
            "final_outcome": "blocked",
            "primary_blocker_gate": "policy_checks",
            "flow_gates": [{"gate_id": "policy_checks", "status": "blocked"}],
            "candidates": [{"strategy_name": "ML_PURE_STAGED", "selected": False, "ordered_gates": []}],
        }


class LiveStrategyAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live = dashboard_app._live_strategy_monitor_service
        self._fake_live = _FakeLiveMonitorService()
        dashboard_app._live_strategy_monitor_service = self._fake_live

    def tearDown(self) -> None:
        dashboard_app._live_strategy_monitor_service = self._old_live

    def test_live_strategy_page_renders(self) -> None:
        request = type("RequestStub", (), {"scope": {"type": "http"}})()
        response = asyncio.run(dashboard_app.live_strategy(request))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Live Strategy Monitor", response.body)
        self.assertIn(b"Live Drilldown", response.body)
        self.assertIn(b"Mode Rail", response.body)
        self.assertIn(b"Live Watchlist", response.body)
        self.assertIn(b"Session Trades", response.body)
        self.assertIn(b"Evaluation Compare", response.body)
        self.assertIn(b"Research Explorer", response.body)

    def test_live_strategy_session_endpoint_returns_payload(self) -> None:
        payload = asyncio.run(dashboard_app.get_live_strategy_session(date="2026-03-02"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["capital"]["configured_capital"], 500000.0)
        self.assertEqual(payload["recent_trades"][0]["capital_at_risk"], 13830.0)
        self.assertEqual(payload["promotion_lane"], "ml_pure")
        self.assertEqual(payload["engine_context"]["active_engine_mode"], "ml_pure")
        self.assertIn("ml_pure", payload["decision_diagnostics"])
        self.assertIn("ops_state", payload)
        self.assertIn("active_alerts", payload)
        self.assertIn("decision_explainability", payload)
        self.assertIn("ui_hints", payload)

    def test_live_strategy_session_forwards_timeline_and_debug_params(self) -> None:
        asyncio.run(
            dashboard_app.get_live_strategy_session(
                date="2026-03-02",
                timeline_limit=40,
                debug_view=1,
            )
        )
        self.assertEqual(self._fake_live.last_kwargs.get("timeline_limit"), 40)
        self.assertEqual(self._fake_live.last_kwargs.get("debug_view"), 1)

    def test_live_strategy_traces_endpoint_returns_rows(self) -> None:
        payload = asyncio.run(dashboard_app.get_live_strategy_traces(date="2026-03-02", limit=10, only_blocked=1))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rows"][0]["trace_id"], "trace-1")
        self.assertEqual(self._fake_live.last_kwargs.get("only_blocked"), True)

    def test_live_strategy_trace_detail_endpoint_returns_trace(self) -> None:
        payload = asyncio.run(dashboard_app.get_live_strategy_trace_detail("trace-1"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["trace"]["trace_id"], "trace-1")

    def test_live_strategy_session_matches_snapshot_fixture(self) -> None:
        payload = asyncio.run(dashboard_app.get_live_strategy_session(date="2026-03-02"))
        fixture_path = Path(__file__).resolve().parent / "tests" / "fixtures" / "live_strategy_session_snapshot.json"
        if not fixture_path.exists():
            fixture_path = Path(__file__).resolve().parent / "fixtures" / "live_strategy_session_snapshot.json"
        expected = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, expected)

    def test_normalize_timestamp_fields_does_not_touch_capital_at_risk(self) -> None:
        normalized = dashboard_app._normalize_timestamp_fields(
            {
                "capital_at_risk": 13830.0,
                "exit_time": "2026-03-02 07:17:00",
                "nested": {"updated_at": "2026-03-02 07:18:00"},
            }
        )

        self.assertEqual(normalized["capital_at_risk"], 13830.0)
        self.assertEqual(normalized["exit_time"], "2026-03-02T07:17:00+05:30")
        self.assertEqual(normalized["nested"]["updated_at"], "2026-03-02T07:18:00+05:30")

    def test_health_endpoint_reports_dependency_summary(self) -> None:
        old_strategy = dashboard_app._strategy_eval_service
        dashboard_app._strategy_eval_service = object()
        try:
            with mock.patch.object(
                dashboard_app._operator_routes,
                "_probe_market_data_health",
                return_value={
                    "status": "healthy",
                    "reachable": True,
                    "status_code": 200,
                    "latency_ms": 12.3,
                    "url": "http://localhost:8004/health",
                    "timestamp": "2026-03-02T07:17:00+05:30",
                    "error": None,
                },
            ), mock.patch.object(
                dashboard_app._operator_routes,
                "_probe_redis",
                return_value={
                    "status": "healthy",
                    "reachable": True,
                    "host": "localhost",
                    "port": 6379,
                    "latency_ms": 1.2,
                    "error": None,
                },
            ):
                payload = asyncio.run(dashboard_app.health())
        finally:
            dashboard_app._strategy_eval_service = old_strategy

        self.assertEqual(payload["status"], "healthy")
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["checks"]["strategy_evaluation_service"])
        self.assertTrue(payload["checks"]["live_strategy_monitor_service"])
        self.assertEqual(payload["dependencies"]["market_data_api"]["status"], "healthy")
        self.assertEqual(payload["dependencies"]["redis"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
