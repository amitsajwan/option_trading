import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import market_data_dashboard.app as dashboard_app


class PublicContractRouteTests(unittest.TestCase):
    def test_public_contract_market_data_handlers_are_bound_to_market_data_router(self) -> None:
        self.assertIs(dashboard_app._public_contract_routes._get_ohlc_data.__self__, dashboard_app._market_data_routes)
        self.assertIs(
            dashboard_app._public_contract_routes._get_technical_indicators.__self__,
            dashboard_app._market_data_routes,
        )
        self.assertIs(dashboard_app._public_contract_routes._get_market_depth.__self__, dashboard_app._market_data_routes)
        self.assertIs(dashboard_app._public_contract_routes._get_options_chain.__self__, dashboard_app._market_data_routes)

    def test_get_public_schema_index_alias_lists_available_topics(self) -> None:
        fake_schemas = {
            "tick": {"type": "object"},
            "mode": {"type": "object"},
        }
        with patch.object(dashboard_app, "_public_topic_schemas", return_value=fake_schemas):
            payload = asyncio.run(dashboard_app.get_public_schema_index())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["schema_version"], dashboard_app.PUBLIC_SCHEMA_VERSION)
        self.assertEqual([item["topic"] for item in payload["topics"]], ["tick", "mode"])

    def test_get_public_capabilities_alias_delegates_runtime_catalog(self) -> None:
        fake_catalog = {
            "status": "ok",
            "mode": "live",
            "instrument": "NIFTY-I",
            "instruments": ["NIFTY-I"],
            "availability": {"tick": True},
            "apis": {"tick": "/api/v1/market/tick/NIFTY-I"},
            "ws_topics": {"tick": "tick"},
        }
        with patch.object(dashboard_app, "_build_runtime_catalog", new=AsyncMock(return_value=fake_catalog)):
            payload = asyncio.run(dashboard_app.get_public_capabilities(instrument="NIFTY-I"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["default_instrument"], "NIFTY-I")
        self.assertEqual(payload["apis"]["tick"], "/api/v1/market/tick/NIFTY-I")

    def test_get_public_topic_example_mode_alias_uses_system_mode(self) -> None:
        with patch.object(dashboard_app, "_load_runtime_instruments", new=AsyncMock(return_value=[])), patch.object(
            dashboard_app,
            "get_system_mode",
            new=AsyncMock(return_value={"status": "ok", "mode": "replay"}),
        ), patch.object(
            dashboard_app,
            "_get_current_mode_hint",
            return_value="replay",
        ):
            payload = asyncio.run(dashboard_app.get_public_topic_example(topic="mode"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["topic"], "mode")
        self.assertEqual(payload["mode"], "replay")
        self.assertEqual(payload["sample"]["mode"], "replay")

    def test_get_public_topic_example_ohlc_alias_uses_market_data_handler(self) -> None:
        ohlc_mock = AsyncMock(return_value=[{"start_at": "2026-03-21T09:15:00+05:30", "close": 100.5}])
        with patch.object(dashboard_app, "_load_runtime_instruments", new=AsyncMock(return_value=["NIFTY-I"])), patch.object(
            dashboard_app._public_contract_routes,
            "_get_ohlc_data",
            new=ohlc_mock,
        ), patch.object(
            dashboard_app,
            "_get_current_mode_hint",
            return_value="live",
        ):
            payload = asyncio.run(dashboard_app.get_public_topic_example(topic="ohlc", instrument="NIFTY-I"))

        ohlc_mock.assert_awaited_once_with(
            instrument="NIFTY-I",
            timeframe="1min",
            limit=3,
            order="desc",
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["topic"], "ohlc")
        self.assertEqual(payload["sample"][0]["close"], 100.5)

    def test_get_public_topic_example_market_data_topics_use_aliases(self) -> None:
        indicators_mock = AsyncMock(return_value={"status": "ok", "indicators": {"ema_20": 101.2}})
        depth_mock = AsyncMock(return_value={"status": "ok", "buy": [{"price": 100.0}], "sell": [{"price": 100.5}]})
        options_mock = AsyncMock(return_value={"status": "synthetic", "strikes": [{"strike": 22000}]})

        with patch.object(dashboard_app, "_load_runtime_instruments", new=AsyncMock(return_value=["NIFTY-I"])), patch.object(
            dashboard_app._public_contract_routes,
            "_get_technical_indicators",
            new=indicators_mock,
        ), patch.object(
            dashboard_app._public_contract_routes,
            "_get_market_depth",
            new=depth_mock,
        ), patch.object(
            dashboard_app._public_contract_routes,
            "_get_options_chain",
            new=options_mock,
        ), patch.object(
            dashboard_app,
            "_get_current_mode_hint",
            return_value="live",
        ):
            indicators_payload = asyncio.run(
                dashboard_app.get_public_topic_example(topic="indicators", instrument="NIFTY-I")
            )
            depth_payload = asyncio.run(dashboard_app.get_public_topic_example(topic="depth", instrument="NIFTY-I"))
            options_payload = asyncio.run(dashboard_app.get_public_topic_example(topic="options", instrument="NIFTY-I"))

        indicators_mock.assert_awaited_once_with(instrument="NIFTY-I", timeframe="1min")
        depth_mock.assert_awaited_once_with("NIFTY-I")
        options_mock.assert_awaited_once_with("NIFTY-I")
        self.assertEqual(indicators_payload["sample"]["indicators"]["ema_20"], 101.2)
        self.assertEqual(depth_payload["sample"]["buy"][0]["price"], 100.0)
        self.assertEqual(options_payload["sample"]["status"], "synthetic")


if __name__ == "__main__":
    unittest.main()
