import asyncio
import json
import unittest
from unittest.mock import patch

import market_data_dashboard.app as dashboard_app


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeRedis:
    def ping(self):
        return True

    def zcard(self, key):
        return 3 if "ohlc_sorted" in str(key) else 0

    def zrange(self, key, start, end):
        bars = [
            json.dumps({"start_at": "2026-03-21T09:15:00+05:30", "close": 100.0}),
            json.dumps({"start_at": "2026-03-21T09:16:00+05:30", "close": 101.5}),
        ]
        if start == 0 and end == 0:
            return [bars[0]]
        if start == -1 and end == -1:
            return [bars[-1]]
        return bars


class MarketDataStatusContractTests(unittest.TestCase):
    def test_market_data_status_reports_contract_and_available_instrument(self) -> None:
        def _fake_get(url, timeout=0, **kwargs):
            if str(url).endswith("/health"):
                return _FakeResponse({"status": "healthy", "mode": "live"})
            if str(url).endswith("/api/v1/market/instruments"):
                return _FakeResponse({"instruments": ["NIFTY"]})
            raise AssertionError(f"unexpected url {url}")

        with patch.object(dashboard_app.requests, "get", side_effect=_fake_get), patch.object(
            dashboard_app,
            "_redis_sync_client",
            return_value=_FakeRedis(),
        ), patch.object(
            dashboard_app,
            "_ohlc_sorted_keys_to_try",
            return_value=["live:ohlc_sorted:NIFTY:1min"],
        ), patch.object(
            dashboard_app,
            "_normalize_timestamp_fields",
            side_effect=lambda value: value,
        ), patch.object(
            dashboard_app,
            "validate_data_availability",
            return_value={"overall_status": "healthy", "checks": {"api_health": True}},
        ), patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-21T09:30:00+05:30",
        ):
            payload = asyncio.run(dashboard_app.market_data_status())

        self.assertEqual(payload["timestamp"], "2026-03-21T09:30:00+05:30")
        self.assertEqual(payload["market_data_api"]["status"], "healthy")
        self.assertEqual(payload["redis"]["status"], "healthy")
        self.assertIn("NIFTY", payload["instruments"])
        self.assertEqual(payload["instruments"]["NIFTY"]["status"], "available")
        self.assertEqual(payload["instruments"]["NIFTY"]["data_points"], 3)
        self.assertEqual(payload["data_validation"]["overall_status"], "healthy")

    def test_sync_lag_reports_domain_checks_and_summary(self) -> None:
        lag_calls = []

        def _fake_lag_check_payload(**kwargs):
            lag_calls.append(kwargs["name"])
            return {
                "status": "ok",
                "name": kwargs["name"],
                "redis_source": kwargs.get("redis_source"),
                "mongo_source": kwargs.get("mongo_source"),
            }

        with patch.object(dashboard_app, "_normalize_instrument_symbol", return_value="NIFTY"), patch.object(
            dashboard_app,
            "_get_current_mode_hint",
            return_value="live",
        ), patch.object(
            dashboard_app,
            "_redis_sync_client",
            return_value=_FakeRedis(),
        ), patch.object(
            dashboard_app,
            "_read_ohlc_from_redis",
            return_value=([{"start_at": "2026-03-21T09:15:00+05:30"}], "live:ohlc_sorted:NIFTY:1min"),
        ), patch.object(
            dashboard_app,
            "_load_latest_snapshot_from_mongo",
            return_value={"snapshot_timestamp": "2026-03-21T09:15:30+05:30"},
        ), patch.object(
            dashboard_app,
            "_redis_prefixed_keys",
            side_effect=lambda mode_hint, keys: list(keys),
        ), patch.object(
            dashboard_app,
            "_redis_get_first_value",
            side_effect=[
                ("tick:NIFTY:latest", json.dumps({"timestamp": "2026-03-21T09:15:10+05:30"})),
                ("depth:NIFTY:timestamp", "2026-03-21T09:15:20+05:30"),
                ("options:NIFTY:chain", json.dumps({"timestamp": "2026-03-21T09:15:25+05:30"})),
            ],
        ), patch.object(
            dashboard_app,
            "_mongo_latest_ts_for_instrument",
            side_effect=[
                {"timestamp": "2026-03-21T09:15:11+05:30", "collection": "live_ticks", "collection_exists": True},
                {"timestamp": "2026-03-21T09:15:21+05:30", "collection": "live_depth", "collection_exists": True},
                {"timestamp": "2026-03-21T09:15:26+05:30", "collection": "live_options_chain", "collection_exists": True},
            ],
        ), patch.object(
            dashboard_app,
            "_scan_keys_limited",
            return_value=[],
        ), patch.object(
            dashboard_app,
            "_safe_json_loads",
            side_effect=lambda raw: json.loads(raw) if raw else None,
        ), patch.object(
            dashboard_app,
            "_mode_priority",
            return_value=["live"],
        ), patch.object(
            dashboard_app,
            "_lag_check_payload",
            side_effect=_fake_lag_check_payload,
        ), patch.object(
            dashboard_app,
            "_normalize_timestamp_fields",
            side_effect=lambda value: value,
        ), patch.object(
            dashboard_app,
            "_now_iso_ist",
            return_value="2026-03-21T09:30:00+05:30",
        ):
            payload = asyncio.run(dashboard_app.get_redis_mongo_sync_lag("NIFTY"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["timestamp"], "2026-03-21T09:30:00+05:30")
        self.assertEqual(payload["instrument"], "NIFTY")
        self.assertTrue(payload["redis"]["ok"])
        self.assertEqual(sorted(payload["checks"].keys()), ["depth", "options", "snapshot", "tick"])
        self.assertEqual(payload["summary"]["ok"], 4)
        self.assertEqual(payload["summary"]["lagging"], 0)
        self.assertEqual(lag_calls, ["snapshot", "tick", "depth", "options"])


if __name__ == "__main__":
    unittest.main()
