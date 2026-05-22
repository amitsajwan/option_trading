import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import market_data_dashboard.app as dashboard_app


class MarketDataRouteTests(unittest.TestCase):
    def test_get_available_instruments_fallback_keeps_full_contract(self) -> None:
        with patch.object(dashboard_app.requests, "get", side_effect=RuntimeError("upstream unavailable")), patch.object(
            dashboard_app,
            "_discover_instruments_from_redis",
            return_value=["BANKNIFTY", "NIFTY"],
        ), patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-21T09:30:00+05:30",
        ):
            payload = asyncio.run(dashboard_app.get_available_instruments())

        self.assertEqual(payload["instruments"], ["BANKNIFTY", "NIFTY"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["timestamp"], "2026-03-21T09:30:00+05:30")

    def test_get_available_instruments_filters_placeholders_on_success(self) -> None:
        class _FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"instruments": ["NIFTY", "", "SELECT_INSTRUMENT", "BANKNIFTY"]}

        with patch.object(dashboard_app.requests, "get", return_value=_FakeResponse()), patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-21T09:30:00+05:30",
        ):
            payload = asyncio.run(dashboard_app.get_available_instruments())

        self.assertEqual(payload["instruments"], ["NIFTY", "BANKNIFTY"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["timestamp"], "2026-03-21T09:30:00+05:30")

    def test_get_chart_data_uses_ohlc_alias_and_builds_chart_payload(self) -> None:
        fake_ohlc = [{"timestamp": "2026-03-21T09:15:00+05:30", "close": 100.5}]
        fake_payload = {"series": [{"name": "close", "points": [100.5]}]}
        ohlc_mock = AsyncMock(return_value=fake_ohlc)

        with patch.object(dashboard_app, "get_ohlc_data", ohlc_mock), patch.object(
            dashboard_app,
            "_build_chart_payload_from_ohlc",
            return_value=dict(fake_payload),
        ) as build_payload, patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-21T09:30:00+05:30",
        ), patch.object(
            dashboard_app,
            "_normalize_timestamp_fields",
            side_effect=lambda value: value,
        ):
            payload = asyncio.run(dashboard_app.get_chart_data("NIFTY", timeframe="2min", limit=7))

        ohlc_mock.assert_awaited_once_with(
            instrument="NIFTY",
            timeframe="2min",
            limit=120,
            order="asc",
        )
        build_payload.assert_called_once_with(
            instrument="NIFTY",
            timeframe="2min",
            ohlc_data=fake_ohlc,
            req_limit=7,
            indicators_bars_needed=120,
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["timestamp"], "2026-03-21T09:30:00+05:30")
        self.assertEqual(payload["series"][0]["points"], [100.5])


if __name__ == "__main__":
    unittest.main()
