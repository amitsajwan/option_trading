import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

import market_data_dashboard.app as dashboard_app


class _FakeHistoricalReplayService:
    def __init__(self) -> None:
        self.last_session_kwargs = {}
        self.last_status_kwargs = {}

    def get_historical_strategy_session(self, **kwargs: object) -> dict:
        self.last_session_kwargs = dict(kwargs)
        return {
            "mode": "historical",
            "session": {
                "date_ist": "2026-03-06",
                "instrument": "BANKNIFTY26MARFUT",
                "latest_event_time": "2026-03-06T10:15:00+05:30",
                "market_session_open": True,
                "dataset": "historical",
            },
            "capital": {
                "configured_capital": 500000.0,
                "realized_pnl_pct": 0.012,
            },
            "counts": {
                "open_positions": 1,
                "closed_trades": 3,
            },
            "recent_trades": [],
            "recent_signals": [],
            "recent_votes": [],
            "session_chart": {"labels": ["09:15"], "prices": [50200.0]},
            "replay_status": self.get_replay_status(),
        }

    def get_replay_status(self, **kwargs: object) -> dict:
        self.last_status_kwargs = dict(kwargs)
        return {
            "mode": "historical",
            "dataset": "historical",
            "topic": "market:snapshot:v1:historical",
            "date_ist": "2026-03-06",
            "start_date": "2026-03-06",
            "end_date": "2026-03-06",
            "speed": 0.0,
            "events_emitted": 375,
            "cycles": 1,
            "current_replay_timestamp": "2026-03-06T10:15:00+05:30",
            "current_trade_date": "2026-03-06",
            "virtual_time_enabled": True,
            "virtual_time_current": "2026-03-06T10:15:00+05:30",
            "data_ready": True,
            "completed": True,
            "status": "complete",
            "collection_counts": {"votes": 40, "signals": 4, "positions": 6},
        }


class HistoricalReplayAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_service = dashboard_app._historical_replay_monitor_service
        self._fake_service = _FakeHistoricalReplayService()
        dashboard_app._historical_replay_monitor_service = self._fake_service

    def tearDown(self) -> None:
        dashboard_app._historical_replay_monitor_service = self._old_service

    def test_historical_replay_page_renders(self) -> None:
        request = type("RequestStub", (), {"scope": {"type": "http"}})()
        response = asyncio.run(dashboard_app.historical_replay(request))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Historical Replay Monitor", response.body)

    def test_historical_replay_session_endpoint_returns_payload(self) -> None:
        payload = asyncio.run(dashboard_app.get_historical_strategy_session(date="2026-03-06"))
        self.assertEqual(payload["mode"], "historical")
        self.assertEqual(payload["session"]["dataset"], "historical")
        self.assertEqual(payload["replay_status"]["topic"], "market:snapshot:v1:historical")
        self.assertEqual(self._fake_service.last_session_kwargs.get("date"), "2026-03-06")

    def test_historical_replay_status_endpoint_returns_payload(self) -> None:
        payload = asyncio.run(dashboard_app.get_historical_replay_status(date="2026-03-06"))
        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["data_ready"])
        self.assertEqual(self._fake_service.last_status_kwargs.get("date"), "2026-03-06")

    def test_replay_health_uses_replay_status(self) -> None:
        payload = asyncio.run(dashboard_app.replay_health(date="2026-03-06"))
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["mode"], "historical")
        self.assertEqual(payload["replay"]["collection_counts"]["signals"], 4)

    def test_historical_monitor_service_can_exist_without_strategy_eval_service(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")
        service = dashboard_app.HistoricalReplayMonitorService(None)
        self.assertIsNotNone(service)

    def test_top_level_app_import_exposes_historical_monitor_service(self) -> None:
        app_path = Path(dashboard_app.__file__).resolve()
        spec = importlib.util.spec_from_file_location("dashboard_top_level_app", app_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        top_level_app = importlib.util.module_from_spec(spec)
        module_dir = str(app_path.parent)
        old_sys_path = list(sys.path)
        try:
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
            spec.loader.exec_module(top_level_app)
        finally:
            sys.path[:] = old_sys_path
        self.assertIsNotNone(top_level_app.HistoricalReplayMonitorService)


if __name__ == "__main__":
    unittest.main()
