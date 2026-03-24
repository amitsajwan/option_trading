import asyncio
import unittest
from unittest.mock import patch

import market_data_dashboard.app as dashboard_app


class _FakeRedis:
    def get(self, key):
        return None


class HistoricalMarketDataTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_app._LAST_GOOD_OPTIONS.clear()
        dashboard_app._LAST_GOOD_DEPTH.clear()

    def test_get_market_depth_historical_skips_upstream_api(self) -> None:
        with patch.object(dashboard_app, "_get_current_mode_hint", return_value="historical"), patch.object(
            dashboard_app.requests,
            "get",
            side_effect=AssertionError("historical depth should not call upstream api"),
        ), patch.object(
            dashboard_app.redis,
            "Redis",
            return_value=_FakeRedis(),
        ), patch.object(
            dashboard_app,
            "_normalize_depth_contract",
            side_effect=lambda instrument, payload, **kwargs: payload,
        ), patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-25T00:00:00+05:30",
        ):
            payload = asyncio.run(dashboard_app.get_market_depth("BANKNIFTY-I"))

        self.assertEqual(payload["status"], "no_data")
        self.assertIsNone(payload.get("warning"))

    def test_get_options_chain_historical_uses_synthetic_without_upstream_api(self) -> None:
        with patch.object(dashboard_app, "_get_current_mode_hint", return_value="historical"), patch.object(
            dashboard_app.requests,
            "get",
            side_effect=AssertionError("historical options should not call upstream api"),
        ), patch.object(
            dashboard_app.redis,
            "Redis",
            return_value=_FakeRedis(),
        ), patch.object(
            dashboard_app,
            "_allow_synthetic_fallback",
            return_value=True,
        ), patch.object(
            dashboard_app,
            "_build_synthetic_options_chain_black_scholes",
            return_value={"instrument": "BANKNIFTY-I", "strikes": [{"strike": 50000}], "status": "synthetic"},
        ), patch.object(
            dashboard_app,
            "_normalize_options_contract",
            side_effect=lambda instrument, payload, **kwargs: payload,
        ):
            payload = asyncio.run(dashboard_app.get_options_chain("BANKNIFTY-I"))

        self.assertEqual(payload["status"], "synthetic")
        self.assertEqual(len(payload["strikes"]), 1)

    def test_get_options_chain_historical_uses_snapshot_payload_when_available(self) -> None:
        with patch.object(dashboard_app, "_get_current_mode_hint", return_value="historical"), patch.object(
            dashboard_app.requests,
            "get",
            side_effect=AssertionError("historical options should not call upstream api"),
        ), patch.object(
            dashboard_app.redis,
            "Redis",
            return_value=_FakeRedis(),
        ), patch.object(
            dashboard_app,
            "_historical_options_payload_from_snapshot",
            return_value={
                "instrument": "BANKNIFTY-I",
                "timestamp": "2024-10-31T15:30:00+05:30",
                "status": "ok",
                "strikes": [{"strike": 52200, "ce_oi": 350415, "pe_oi": 200295}],
                "futures_price": 52213.55,
                "pcr": 1.07,
                "max_pain": 52000,
            },
        ), patch.object(
            dashboard_app,
            "_normalize_options_contract",
            side_effect=lambda instrument, payload, **kwargs: payload,
        ):
            payload = asyncio.run(dashboard_app.get_options_chain("BANKNIFTY-I"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["instrument"], "BANKNIFTY-I")
        self.assertEqual(payload["max_pain"], 52000)

    def test_mode_hint_falls_back_to_historical_from_redis_replay_state(self) -> None:
        class _ReplayRedis:
            def get(self, key):
                mapping = {
                    "system:virtual_time:enabled": "1",
                    "system:historical:data_ready": "1",
                    "system:historical:replay_status": '{"mode":"historical","status":"complete"}',
                }
                return mapping.get(key)

        with patch.object(
            dashboard_app.requests,
            "get",
            side_effect=RuntimeError("upstream mode api unavailable"),
        ), patch.object(
            dashboard_app,
            "_redis_sync_client",
            return_value=_ReplayRedis(),
        ):
            mode = dashboard_app._get_current_mode_hint()

        self.assertEqual(mode, "historical")


if __name__ == "__main__":
    unittest.main()
