import asyncio
import unittest

import market_data_dashboard.app as dashboard_app


class _RequestStub:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeStrategyEvaluationService:
    def __init__(self) -> None:
        self.last_filters = None
        self.last_queue_run = None

    def parse_filters(self, **kwargs):
        self.last_filters = dict(kwargs)
        return {
            "dataset": str(kwargs.get("dataset") or "historical"),
            "date_from": str(kwargs.get("date_from") or ""),
            "date_to": str(kwargs.get("date_to") or ""),
            "strategies": [kwargs["strategy_raw"]] if kwargs.get("strategy_raw") else [],
            "regimes": [kwargs["regime_raw"]] if kwargs.get("regime_raw") else [],
            "initial_capital": float(kwargs.get("initial_capital") or 0.0),
            "cost_bps": float(kwargs.get("cost_bps") or 0.0),
            "page": int(kwargs.get("page") or 1),
            "page_size": int(kwargs.get("page_size") or 50),
            "sort_by": str(kwargs.get("sort_by") or "exit_time"),
            "sort_dir": str(kwargs.get("sort_dir") or "desc"),
            "run_id": kwargs.get("run_id_raw"),
        }

    def compute_summary(self, **kwargs):
        return {
            "status": "ok",
            "generated_at": "2026-03-02 07:17:00",
            "filters": kwargs,
        }

    def queue_replay_run(self, **kwargs):
        self.last_queue_run = dict(kwargs)
        return {
            "run_id": "run_123",
            "submitted_at": "2026-03-02 07:18:00",
            "status": "queued",
        }

    def get_latest_run(self, **kwargs):
        return {
            "run_id": "run_latest",
            "updated_at": "2026-03-02 07:19:00",
            "status": kwargs.get("status") or "completed",
        }

    def get_run(self, run_id: str):
        if run_id == "missing":
            return None
        return {
            "run_id": run_id,
            "updated_at": "2026-03-02 07:20:00",
            "status": "completed",
        }


class StrategyEvaluationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_service = dashboard_app._strategy_eval_service
        self._fake_service = _FakeStrategyEvaluationService()
        dashboard_app._strategy_eval_service = self._fake_service

    def tearDown(self) -> None:
        dashboard_app._strategy_eval_service = self._old_service

    def test_summary_alias_delegates_and_normalizes_timestamps(self) -> None:
        payload = asyncio.run(
            dashboard_app.get_strategy_evaluation_summary(
                dataset="live",
                date_from="2026-03-01",
                date_to="2026-03-02",
                strategy="ORB",
                regime="trend",
                run_id="run_7",
                initial_capital=2500.0,
                cost_bps=3.5,
            )
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["generated_at"], "2026-03-02T07:17:00+05:30")
        self.assertEqual(self._fake_service.last_filters["dataset"], "live")
        self.assertEqual(self._fake_service.last_filters["strategy_raw"], "ORB")
        self.assertEqual(self._fake_service.last_filters["run_id_raw"], "run_7")

    def test_create_run_alias_delegates_and_normalizes_submission_time(self) -> None:
        payload = asyncio.run(
            dashboard_app.create_strategy_evaluation_run(
                _RequestStub(
                    {
                        "dataset": "historical",
                        "date_from": "2026-03-01",
                        "date_to": "2026-03-02",
                        "speed": 2.0,
                        "base_path": "artifacts/replay",
                        "stop_loss_pct": 0.15,
                    }
                )
            )
        )

        self.assertEqual(payload["run_id"], "run_123")
        self.assertEqual(payload["submitted_at"], "2026-03-02T07:18:00+05:30")
        self.assertEqual(self._fake_service.last_queue_run["dataset"], "historical")
        self.assertEqual(self._fake_service.last_queue_run["base_path"], "artifacts/replay")
        self.assertEqual(self._fake_service.last_queue_run["risk_config"]["stop_loss_pct"], 0.15)

    def test_latest_run_alias_uses_service_and_normalizes_timestamp(self) -> None:
        payload = asyncio.run(
            dashboard_app.get_latest_strategy_evaluation_run(dataset="historical", status="completed")
        )

        self.assertEqual(payload["run_id"], "run_latest")
        self.assertEqual(payload["updated_at"], "2026-03-02T07:19:00+05:30")


if __name__ == "__main__":
    unittest.main()
