import asyncio
import json
import unittest
from pathlib import Path

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
                "strategy_family_version": "ML_PURE_DUAL_V1",
                "strategy_profile_id": "ml_pure_dual_v1",
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
                "ml_gate": {"status": "NO_ML_SAMPLE_TODAY"},
            },
            "ml_diagnostics": {"status": "NO_ML_SAMPLE_TODAY"},
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
                    "source": "decision_diagnostics.ml_gate",
                    "operator_next_step": "No action unless warmup extends beyond expected window.",
                }
            ],
            "decision_explainability": {
                "latest_decision": {
                    "id": "signal:s1",
                    "ts": "2026-03-02T07:17:00Z",
                    "engine_mode": "ml_pure",
                    "decision_mode": "ml_dual",
                    "action": "HOLD",
                    "reason_code": "low_edge_conflict",
                    "explanation": "Edge is insufficient.",
                    "operator_hint": "Wait for stronger edge.",
                    "metrics": {"edge": 0.01, "confidence": 0.62},
                    "source_ref": "signal:s1",
                    "gate_path": "Candidate -> ML Dual -> Risk/Phase -> HOLD",
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
            "chart_markers": [],
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


if __name__ == "__main__":
    unittest.main()
