"""Unit tests for depth_collector — 5-level ladder, derived metrics, Mongo record shape."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ingestion_app.collectors.depth_collector import (
    _build_record,
    _compute_derived,
    _normalize_levels,
    _poll_once,
    _redis_payload,
    _side_key,
)


class NormalizeLevelsTests(unittest.TestCase):
    def test_pads_to_five_levels(self) -> None:
        out = _normalize_levels([
            {"price": 100.0, "quantity": 50, "orders": 2},
            {"price": 99.5, "quantity": 75, "orders": 3},
        ])
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0], {"price": 100.0, "qty": 50, "orders": 2})
        self.assertEqual(out[1], {"price": 99.5, "qty": 75, "orders": 3})
        self.assertEqual(out[2], {"price": 0.0, "qty": 0, "orders": 0})

    def test_handles_missing_fields(self) -> None:
        out = _normalize_levels([{"price": 100.0}])  # no qty/orders
        self.assertEqual(out[0], {"price": 100.0, "qty": 0, "orders": 0})

    def test_caps_at_five_levels(self) -> None:
        out = _normalize_levels([{"price": 100.0 - i, "quantity": 10, "orders": 1} for i in range(8)])
        self.assertEqual(len(out), 5)

    def test_skips_malformed_levels(self) -> None:
        out = _normalize_levels([
            {"price": "not-a-number", "quantity": 10},
            {"price": 99.5, "quantity": 50, "orders": 1},
        ])
        self.assertEqual(out[0], {"price": 99.5, "qty": 50, "orders": 1})


class ComputeDerivedTests(unittest.TestCase):
    def _ladder(self, bids, asks):
        bid_lvls = _normalize_levels([{"price": p, "quantity": q, "orders": 1} for p, q in bids])
        ask_lvls = _normalize_levels([{"price": p, "quantity": q, "orders": 1} for p, q in asks])
        return _compute_derived(bid_lvls, ask_lvls)

    def test_basic_spread_and_mid(self) -> None:
        d = self._ladder([(100.0, 50)], [(100.5, 50)])
        self.assertAlmostEqual(d["spread"], 0.5)
        self.assertAlmostEqual(d["mid"], 100.25)
        self.assertAlmostEqual(d["microprice"], 100.25, places=2)  # equal qty → mid

    def test_microprice_skews_toward_thicker_side(self) -> None:
        # Tiny ask, huge bid → microprice should sit closer to the ask (which gets weight from large bid_qty)
        d = self._ladder([(100.0, 1000)], [(100.5, 10)])
        self.assertGreater(d["microprice"], d["mid"])

    def test_qty_imbalance_bullish(self) -> None:
        d = self._ladder([(100.0, 500), (99.5, 800)], [(100.5, 100), (101.0, 100)])
        # Bid total 1300 vs Ask total 200 → strongly bullish
        self.assertEqual(d["total_bid_qty"], 1300)
        self.assertEqual(d["total_ask_qty"], 200)
        self.assertGreater(d["qty_imbalance"], 0.5)

    def test_qty_imbalance_bearish(self) -> None:
        d = self._ladder([(100.0, 50)], [(100.5, 800), (101.0, 700)])
        self.assertLess(d["qty_imbalance"], -0.5)

    def test_empty_ladder_returns_nones(self) -> None:
        d = self._ladder([], [])
        self.assertIsNone(d["best_bid"])
        self.assertIsNone(d["best_ask"])
        self.assertIsNone(d["spread"])
        self.assertIsNone(d["microprice"])


class SideKeyTests(unittest.TestCase):
    def test_ce_maps_to_ce_key(self) -> None:
        self.assertEqual(_side_key("NFO:BANKNIFTY26MAY55700CE"), "depth:atm_ce:latest")

    def test_pe_maps_to_pe_key(self) -> None:
        self.assertEqual(_side_key("NFO:BANKNIFTY26MAY55700PE"), "depth:atm_pe:latest")

    def test_futures_returns_none(self) -> None:
        self.assertIsNone(_side_key("NFO:BANKNIFTY26JUNFUT"))


class BuildRecordTests(unittest.TestCase):
    def test_record_has_full_ladder_and_metrics(self) -> None:
        quote = {
            "last_price": 56.10,
            "volume": 12345,
            "oi": 890000,
            "depth": {
                "buy": [
                    {"price": 56.0, "quantity": 100, "orders": 2},
                    {"price": 55.9, "quantity": 200, "orders": 3},
                ],
                "sell": [
                    {"price": 56.2, "quantity": 80, "orders": 1},
                    {"price": 56.3, "quantity": 150, "orders": 2},
                ],
            },
        }
        rec = _build_record("NFO:BANKNIFTY26MAY55700CE", quote)
        self.assertEqual(rec["instrument"], "NFO:BANKNIFTY26MAY55700CE")
        self.assertEqual(len(rec["bid_levels"]), 5)
        self.assertEqual(len(rec["ask_levels"]), 5)
        self.assertEqual(rec["best_bid"], 56.0)
        self.assertEqual(rec["best_ask"], 56.2)
        self.assertAlmostEqual(rec["spread"], 0.2)
        self.assertEqual(rec["last_price"], 56.10)
        self.assertEqual(rec["volume"], 12345)
        self.assertEqual(rec["oi"], 890000)
        self.assertIn("trade_date_ist", rec)
        self.assertIn("fetched_at", rec)

    def test_redis_payload_excludes_ladder(self) -> None:
        quote = {"depth": {"buy": [{"price": 56.0, "quantity": 100}], "sell": [{"price": 56.2, "quantity": 80}]}}
        rec = _build_record("NFO:BANKNIFTY26MAY55700CE", quote)
        payload = _redis_payload(rec)
        # Back-compat: only flat best-bid/ask fields, no ladder
        self.assertNotIn("bid_levels", payload)
        self.assertNotIn("ask_levels", payload)
        self.assertNotIn("qty_imbalance", payload)
        self.assertEqual(payload["best_bid"], 56.0)
        self.assertEqual(payload["best_ask"], 56.2)


class PollOnceTests(unittest.TestCase):
    def test_writes_redis_and_mongo_for_each_option(self) -> None:
        kite = MagicMock()
        kite.quote.return_value = {
            "NFO:BANKNIFTY26MAY55700CE": {
                "depth": {"buy": [{"price": 56.0, "quantity": 100}], "sell": [{"price": 56.2, "quantity": 80}]},
            },
            "NFO:BANKNIFTY26MAY55700PE": {
                "depth": {"buy": [{"price": 304.0, "quantity": 60}], "sell": [{"price": 305.0, "quantity": 90}]},
            },
        }
        redis_client = MagicMock()
        mongo_coll = MagicMock()

        _poll_once(
            ["NFO:BANKNIFTY26MAY55700CE", "NFO:BANKNIFTY26MAY55700PE"],
            redis_client,
            kite,
            ttl_sec=60,
            mongo_coll=mongo_coll,
        )

        # Two Redis writes (one per instrument)
        self.assertEqual(redis_client.setex.call_count, 2)
        # One batched Mongo write with both records
        self.assertEqual(mongo_coll.insert_many.call_count, 1)
        inserted = mongo_coll.insert_many.call_args[0][0]
        self.assertEqual(len(inserted), 2)
        symbols = {r["instrument"] for r in inserted}
        self.assertEqual(symbols, {"NFO:BANKNIFTY26MAY55700CE", "NFO:BANKNIFTY26MAY55700PE"})

    def test_redis_only_when_mongo_unavailable(self) -> None:
        kite = MagicMock()
        kite.quote.return_value = {
            "NFO:BANKNIFTY26MAY55700CE": {
                "depth": {"buy": [{"price": 56.0, "quantity": 100}], "sell": [{"price": 56.2, "quantity": 80}]},
            },
        }
        redis_client = MagicMock()
        _poll_once(["NFO:BANKNIFTY26MAY55700CE"], redis_client, kite, ttl_sec=60, mongo_coll=None)
        self.assertEqual(redis_client.setex.call_count, 1)

    def test_kite_failure_is_swallowed(self) -> None:
        kite = MagicMock()
        kite.quote.side_effect = RuntimeError("kite api down")
        redis_client = MagicMock()
        mongo_coll = MagicMock()
        _poll_once(["NFO:BANKNIFTY26MAY55700CE"], redis_client, kite, ttl_sec=60, mongo_coll=mongo_coll)
        # No writes attempted when kite call fails
        redis_client.setex.assert_not_called()
        mongo_coll.insert_many.assert_not_called()


if __name__ == "__main__":
    unittest.main()
