import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        self.assertIn(b"Replay Controls", response.body)
        self.assertIn(b"Range Trades", response.body)
        self.assertIn(b"Operator Playbook", response.body)
        self.assertIn(b"Evaluation Compare", response.body)

    def test_historical_replay_session_endpoint_returns_payload(self) -> None:
        payload = asyncio.run(dashboard_app.get_historical_strategy_session(date="2026-03-06"))
        self.assertEqual(payload["mode"], "historical")
        self.assertEqual(payload["session"]["dataset"], "historical")
        self.assertEqual(payload["replay_status"]["topic"], "market:snapshot:v1:historical")
        self.assertEqual(self._fake_service.last_session_kwargs.get("date"), "2026-03-06")

    def test_historical_replay_session_endpoint_forwards_run_id(self) -> None:
        asyncio.run(dashboard_app.get_historical_strategy_session(date="2026-03-06", run_id="run-123"))
        self.assertEqual(self._fake_service.last_session_kwargs.get("run_id"), "run-123")

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

    def test_historical_monitor_service_degrades_without_eval_runs(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")

        class _EvalStub:
            def _load_signal_map(self, **kwargs: object) -> dict:
                return {}

            def compute_trades(self, **kwargs: object) -> dict:
                raise ValueError("no completed historical evaluation runs found")

            def compute_summary(self, **kwargs: object) -> dict:
                raise ValueError("no completed historical evaluation runs found")

        class _RepoStub:
            def collections(self) -> dict:
                marker = object()
                return {"votes": marker, "signals": marker, "positions": marker}

            def snapshot_has_data(self, date_ist, instrument):
                return False

            def latest_snapshot_instrument(self, date_ist):
                return None

        service = dashboard_app.HistoricalReplayMonitorService(None)
        service._evaluation_service = _EvalStub()
        service._repo = _RepoStub()
        service.load_position_map = lambda date_ist, run_id=None: {}  # type: ignore[method-assign]
        service.load_recent_votes = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.load_recent_signals = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.build_decision_diagnostics = lambda **kwargs: {}  # type: ignore[method-assign]
        service.infer_engine_context = lambda **kwargs: {"active_engine_mode": None}  # type: ignore[method-assign]
        service.promotion_lane_from_engine = lambda active_engine_mode: "deterministic"  # type: ignore[method-assign]
        service.build_current_open_positions = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service.build_latest_closed_trade = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service.partition_open_positions = lambda **kwargs: ([], [])  # type: ignore[method-assign]
        service.build_recent_activity = lambda **kwargs: []  # type: ignore[method-assign]
        service.build_freshness = lambda *args: {}  # type: ignore[method-assign]
        service.load_session_underlying_chart = lambda **kwargs: None  # type: ignore[method-assign]
        service.build_chart_markers = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service._is_market_session_open = lambda: False  # type: ignore[method-assign]

        with patch.dict("os.environ", {"LIVE_STRATEGY_UX_V1": "0"}, clear=False):
            payload = service.get_strategy_session(date="2024-10-31")

        self.assertEqual(payload["session"]["date_ist"], "2024-10-31")
        self.assertEqual(payload["capital"]["realized_pnl_amount"], 0.0)
        self.assertEqual(payload["today_summary"]["equity"]["net_return_pct"], 0.0)
        self.assertEqual(payload["recent_trades"], [])

    def test_historical_monitor_service_uses_raw_collection_counts_when_summary_missing(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")

        class _Cursor:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *args, **kwargs):
                return self

            def limit(self, count):
                return _Cursor(self._docs[:count])

            def __iter__(self):
                return iter(self._docs)

        class _Collection:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, query, projection=None):
                docs = [doc for doc in self._docs if str(doc.get("trade_date_ist")) == str(query.get("trade_date_ist"))]
                return _Cursor(docs)

            def count_documents(self, query, limit=0):
                docs = list(self.find(query))
                return len(docs[:limit or None])

        class _RepoStub:
            def __init__(self):
                self._votes = _Collection([])
                self._signals = _Collection(
                    [
                        {"trade_date_ist": "2024-10-31", "timestamp": "2024-10-31T09:45:00+05:30"},
                        {"trade_date_ist": "2024-10-31", "timestamp": "2024-10-31T09:46:00+05:30"},
                    ]
                )
                self._positions = _Collection(
                    [
                        {
                            "trade_date_ist": "2024-10-31",
                            "position_id": "p1",
                            "event": "POSITION_OPEN",
                            "timestamp": "2024-10-31T09:47:00+05:30",
                            "payload": {"position": {"signal_id": "s1", "event": "POSITION_OPEN"}},
                        }
                    ]
                )

            def collections(self) -> dict:
                return {"votes": self._votes, "signals": self._signals, "positions": self._positions}

            def snapshot_has_data(self, date_ist, instrument):
                return False

            def latest_snapshot_instrument(self, date_ist):
                return None

        class _EvalStub:
            def _load_signal_map(self, **kwargs: object) -> dict:
                return {}

            def compute_trades(self, **kwargs: object) -> dict:
                raise ValueError("no completed historical evaluation runs found")

            def compute_summary(self, **kwargs: object) -> dict:
                raise ValueError("no completed historical evaluation runs found")

        service = dashboard_app.HistoricalReplayMonitorService(None)
        service._evaluation_service = _EvalStub()
        service._repo = _RepoStub()
        service.load_position_map = lambda date_ist, run_id=None: {}  # type: ignore[method-assign]
        service.load_recent_votes = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.load_recent_signals = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.build_decision_diagnostics = lambda **kwargs: {}  # type: ignore[method-assign]
        service.infer_engine_context = lambda **kwargs: {"active_engine_mode": "ml_pure"}  # type: ignore[method-assign]
        service.promotion_lane_from_engine = lambda active_engine_mode: "ml_pure"  # type: ignore[method-assign]
        service.build_current_open_positions = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service.build_latest_closed_trade = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service.partition_open_positions = lambda **kwargs: ([], [])  # type: ignore[method-assign]
        service.build_recent_activity = lambda **kwargs: []  # type: ignore[method-assign]
        service.build_freshness = lambda *args: {}  # type: ignore[method-assign]
        service.load_session_underlying_chart = lambda **kwargs: None  # type: ignore[method-assign]
        service.build_chart_markers = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service._is_market_session_open = lambda: False  # type: ignore[method-assign]

        with patch.dict("os.environ", {"LIVE_STRATEGY_UX_V1": "0"}, clear=False):
            payload = service.get_strategy_session(date="2024-10-31")

        self.assertEqual(payload["counts"]["signals"], 2)
        self.assertEqual(payload["counts"]["positions"], 1)
        self.assertEqual(payload["counts"]["votes"], 0)

    def test_historical_monitor_service_falls_back_to_snapshot_instrument_for_chart(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")

        class _Cursor:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *args, **kwargs):
                return self

            def limit(self, count):
                return _Cursor(self._docs[:count])

            def __iter__(self):
                return iter(self._docs)

        class _Collection:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, query, projection=None):
                docs = []
                for doc in self._docs:
                    if str(query.get("trade_date_ist")) != str(doc.get("trade_date_ist")):
                        continue
                    instrument = query.get("instrument")
                    if instrument and str(doc.get("instrument") or "") != str(instrument):
                        continue
                    docs.append(doc)
                return _Cursor(docs)

            def find_one(self, query, projection=None, sort=None):
                docs = list(self.find(query, projection))
                return docs[-1] if docs else None

            def count_documents(self, query, limit=0):
                docs = list(self.find(query))
                return len(docs[:limit or None])

        class _RepoStub:
            def __init__(self):
                self._snapshots = _Collection(
                    [
                        {
                            "trade_date_ist": "2024-10-31",
                            "instrument": "BANKNIFTY-I",
                            "timestamp": "2024-10-31T09:15:00+05:30",
                            "payload": {
                                "snapshot": {
                                    "session_context": {
                                        "timestamp": "2024-10-31T09:15:00+05:30",
                                        "time": "09:15",
                                    },
                                    "futures_bar": {"fut_close": 50200.0},
                                }
                            },
                        }
                    ]
                )

            def collections(self):
                marker = SimpleNamespace(find=lambda *args, **kwargs: _Cursor([]))
                return {"votes": marker, "signals": marker, "positions": marker}

            def snapshot_collection(self):
                return self._snapshots

            def snapshot_has_data(self, date_ist, instrument):
                return False

            def latest_snapshot_instrument(self, date_ist):
                return "BANKNIFTY-I"

        class _EvalStub:
            def _load_signal_map(self, **kwargs: object) -> dict:
                return {}

            def compute_trades(self, **kwargs: object) -> dict:
                return {"rows": []}

            def compute_summary(self, **kwargs: object) -> dict:
                return {
                    "overall": {"trade_count": 0},
                    "equity": {"start_capital": 500000.0, "end_capital": 500000.0, "net_return_pct": 0.0},
                    "by_strategy": [],
                    "by_regime": [],
                    "exit_reasons": [],
                    "streaks": {},
                    "counts": {"signals": 0, "positions": 0, "trades": 0},
                }

        service = dashboard_app.HistoricalReplayMonitorService(None)
        service._evaluation_service = _EvalStub()
        service._repo = _RepoStub()
        service.load_position_map = lambda date_ist, run_id=None: {}  # type: ignore[method-assign]
        service.load_recent_votes = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.load_recent_signals = lambda date_ist, limit, run_id=None: []  # type: ignore[method-assign]
        service.build_decision_diagnostics = lambda **kwargs: {}  # type: ignore[method-assign]
        service.infer_engine_context = lambda **kwargs: {"active_engine_mode": None}  # type: ignore[method-assign]
        service.promotion_lane_from_engine = lambda active_engine_mode: "deterministic"  # type: ignore[method-assign]
        service.build_current_open_positions = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service.build_latest_closed_trade = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service.partition_open_positions = lambda **kwargs: ([], [])  # type: ignore[method-assign]
        service.build_recent_activity = lambda **kwargs: []  # type: ignore[method-assign]
        service.build_freshness = lambda *args: {}  # type: ignore[method-assign]
        service.build_chart_markers = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service._is_market_session_open = lambda: False  # type: ignore[method-assign]

        with patch.dict("os.environ", {"LIVE_STRATEGY_UX_V1": "0"}, clear=False):
            payload = service.get_strategy_session(date="2024-10-31", instrument="BANKNIFTY26MARFUT")

        self.assertEqual(payload["session"]["instrument"], "BANKNIFTY-I")
        self.assertEqual(payload["session_chart"]["prices"], [50200.0])

    def test_historical_replay_status_falls_back_to_latest_completed_run_when_redis_status_is_idle(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")

        class _Collection:
            def __init__(self, count: int) -> None:
                self._count = int(count)

            def count_documents(self, query) -> int:  # noqa: ARG002
                return self._count

        class _SnapshotCollection:
            def find_one(self, query, projection=None, sort=None):  # noqa: ARG002
                return {"timestamp": "2024-01-05T15:30:00+05:30"}

        class _RepoStub:
            def collections(self):
                return {
                    "votes": _Collection(5),
                    "signals": _Collection(2),
                    "positions": _Collection(247),
                }

            def snapshot_collection(self):
                return _SnapshotCollection()

            def latest_trade_date(self):
                return "2024-01-05"

        class _EvalStub:
            def get_latest_run(self, *, dataset="historical", status="completed"):
                self.last_args = {"dataset": dataset, "status": status}
                return {
                    "run_id": "run-123",
                    "status": "completed",
                    "date_from": "2024-01-02",
                    "date_to": "2024-01-05",
                    "started_at": "2024-01-05T10:00:00Z",
                    "ended_at": "2024-01-05T10:01:00Z",
                    "message": "Replay finished: emitted=1503",
                }

        service = dashboard_app.HistoricalReplayMonitorService(None)
        service._evaluation_service = _EvalStub()
        service._repo = _RepoStub()
        service._read_replay_status = lambda: {  # type: ignore[method-assign]
            "status": "idle",
            "topic": "market:snapshot:v1:historical",
            "data_ready": False,
            "virtual_time_enabled": False,
            "virtual_time_current": None,
        }

        payload = service.get_replay_status(date="2024-01-05")

        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["completed"])
        self.assertTrue(payload["data_ready"])
        self.assertEqual(payload["start_date"], "2024-01-02")
        self.assertEqual(payload["end_date"], "2024-01-05")
        self.assertEqual(payload["events_emitted"], 1503)
        self.assertEqual(payload["latest_completed_run_id"], "run-123")

    def test_historical_monitor_service_scopes_session_by_run_id(self) -> None:
        if dashboard_app.HistoricalReplayMonitorService is None:
            self.skipTest("HistoricalReplayMonitorService unavailable in this environment")

        class _Collection:
            def count_documents(self, query, limit=0):  # noqa: ARG002
                return 1

        class _RepoStub:
            def __init__(self):
                self.calls = []

            def collections(self):
                coll = _Collection()
                return {"votes": coll, "signals": coll, "positions": coll}

            def load_recent_votes(self, date_ist, limit, run_id=None):
                self.calls.append(("votes", date_ist, limit, run_id))
                return []

            def load_recent_signals(self, date_ist, limit, run_id=None):
                self.calls.append(("signals", date_ist, limit, run_id))
                return []

            def load_position_map(self, date_ist, run_id=None):
                self.calls.append(("positions", date_ist, run_id))
                return {}

            def snapshot_has_data(self, date_ist, instrument):  # noqa: ARG002
                return False

            def latest_snapshot_instrument(self, date_ist):  # noqa: ARG002
                return None

            def snapshot_collection(self):
                class _SnapshotCollection:
                    def find_one(self, query, projection=None, sort=None):  # noqa: ARG002
                        return None

                return _SnapshotCollection()

        class _EvalStub:
            def __init__(self) -> None:
                self.trade_run_id = None
                self.summary_run_id = None

            def _load_signal_map(self, **kwargs: object) -> dict:
                self.signal_map_query = kwargs.get("date_match")
                return {}

            def compute_trades(self, **kwargs: object) -> dict:
                self.trade_run_id = kwargs.get("run_id")
                return {"rows": []}

            def compute_summary(self, **kwargs: object) -> dict:
                self.summary_run_id = kwargs.get("run_id")
                return {
                    "overall": {"trades": 0},
                    "equity": {"start_capital": 500000.0, "end_capital": 500000.0, "net_return_pct": 0.0},
                    "by_strategy": [],
                    "by_regime": [],
                    "exit_reasons": [],
                    "streaks": {},
                    "counts": {"signals": 0, "positions": 0, "trades": 0, "closed_trades": 0},
                }

        service = dashboard_app.HistoricalReplayMonitorService(None)
        eval_stub = _EvalStub()
        repo_stub = _RepoStub()
        service._evaluation_service = eval_stub
        service._repo = repo_stub
        service.build_decision_diagnostics = lambda **kwargs: {}  # type: ignore[method-assign]
        service.infer_engine_context = lambda **kwargs: {"active_engine_mode": "deterministic"}  # type: ignore[method-assign]
        service.promotion_lane_from_engine = lambda active_engine_mode: "deterministic"  # type: ignore[method-assign]
        service.build_current_open_positions = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service.build_latest_closed_trade = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service.partition_open_positions = lambda **kwargs: ([], [])  # type: ignore[method-assign]
        service.build_recent_activity = lambda **kwargs: []  # type: ignore[method-assign]
        service.build_freshness = lambda *args: {}  # type: ignore[method-assign]
        service.load_session_underlying_chart = lambda **kwargs: None  # type: ignore[method-assign]
        service.build_chart_markers = lambda *args, **kwargs: []  # type: ignore[method-assign]
        service._is_market_session_open = lambda: False  # type: ignore[method-assign]
        service._read_replay_status = lambda: {"status": "completed", "data_ready": True, "topic": "market:snapshot:v1:historical"}  # type: ignore[method-assign]

        with patch.dict("os.environ", {"LIVE_STRATEGY_UX_V1": "0"}, clear=False):
            payload = service.get_historical_strategy_session(date="2024-01-05", run_id="run-123")

        self.assertEqual(payload["active_run_id"], "run-123")
        self.assertEqual(eval_stub.trade_run_id, "run-123")
        self.assertEqual(eval_stub.summary_run_id, "run-123")
        self.assertEqual(eval_stub.signal_map_query, {"trade_date_ist": "2024-01-05", "run_id": "run-123"})
        self.assertIn(("votes", "2024-01-05", 25, "run-123"), repo_stub.calls)
        self.assertIn(("signals", "2024-01-05", 25, "run-123"), repo_stub.calls)
        self.assertIn(("positions", "2024-01-05", "run-123"), repo_stub.calls)

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
