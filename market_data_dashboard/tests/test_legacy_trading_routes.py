import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import market_data_dashboard.app as dashboard_app


class _QueryRequestStub:
    def __init__(self, query_params):
        self.query_params = query_params


class _JsonRequestStub:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class LegacyTradingRouteTests(unittest.TestCase):
    def test_trading_terminal_alias_redirects_prefill_from_catalog(self) -> None:
        request = _QueryRequestStub({"model": "Alpha Model"})
        fake_models = [
            {
                "instance_key": "alpha_model",
                "model_package": "artifacts/model.joblib",
                "threshold_report": "artifacts/thresholds.json",
            }
        ]
        with patch.object(dashboard_app, "_build_trading_model_catalog", return_value=fake_models):
            response = asyncio.run(dashboard_app.trading_terminal(request))

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "/trading?model=alpha_model&model_package=artifacts%2Fmodel.joblib&threshold_report=artifacts%2Fthresholds.json",
        )

    def test_run_trading_backtest_alias_rejects_when_legacy_runtime_disabled(self) -> None:
        request = _JsonRequestStub({"date": "2026-03-20", "instrument": "BANKNIFTY-I"})
        with patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": False, "detail": "disabled"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dashboard_app.run_trading_backtest(request))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "disabled")

    def test_get_latest_backtest_state_rejects_when_legacy_runtime_disabled(self) -> None:
        with patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": False, "detail": "disabled"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dashboard_app.get_latest_backtest_state())

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "disabled")

    def test_get_trading_state_alias_prefers_latest_backtest_ui_state(self) -> None:
        fake_ui_state = {"summary": {"events_count": 4}, "runner": {"view_mode": "backtest"}}
        with patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": True},
        ), patch.object(
            dashboard_app,
            "_load_latest_backtest_state",
            return_value={"ui_state": fake_ui_state},
        ):
            payload = asyncio.run(dashboard_app.get_trading_state(view="backtest"))

        self.assertEqual(payload["summary"]["events_count"], 4)
        self.assertEqual(payload["runner"]["view_mode"], "backtest")

    def test_start_trading_runner_alias_rejects_when_legacy_runtime_disabled(self) -> None:
        request = _JsonRequestStub({"instrument": "BANKNIFTY-I"})
        with patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": False, "detail": "disabled"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dashboard_app.start_trading_runner(request))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "disabled")

    def test_stop_trading_runner_alias_reports_not_running(self) -> None:
        fake_state = {"process": None, "last_exit_code": 9}
        with patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": True},
        ), patch.object(
            dashboard_app,
            "_refresh_trading_runner_state",
            return_value=fake_state,
        ):
            payload = asyncio.run(dashboard_app.stop_trading_runner(instance="Alpha Model"))

        self.assertEqual(payload["status"], "not_running")
        self.assertEqual(payload["instance"], "alpha_model")
        self.assertEqual(payload["last_exit_code"], 9)


if __name__ == "__main__":
    unittest.main()
