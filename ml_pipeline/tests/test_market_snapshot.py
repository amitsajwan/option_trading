import unittest
from datetime import datetime

import pandas as pd

from ml_pipeline.market_snapshot import (
    MarketSnapshotState,
    _merge_ohlc_history,
    build_market_snapshot,
)


def _make_ohlc(start: str, periods: int = 70) -> pd.DataFrame:
    ts = pd.date_range(start=start, periods=periods, freq="min")
    base = 46000.0
    rows = []
    for i, t in enumerate(ts):
        px = base + (i * 2.0)
        rows.append(
            {
                "timestamp": t,
                "open": px - 1.0,
                "high": px + 3.0,
                "low": px - 4.0,
                "close": px,
                "volume": 1000 + (i * 10),
                "oi": 2000000 + (i * 50),
            }
        )
    return pd.DataFrame(rows)


def _make_chain(atm: int = 46100, pcr: float = 1.1, expiry: str = "2024-05-09") -> dict:
    strikes = []
    for strike in (atm - 100, atm, atm + 100):
        strikes.append(
            {
                "strike": strike,
                "ce_ltp": max(10.0, 200.0 - abs(strike - atm) * 0.5),
                "pe_ltp": max(10.0, 180.0 - abs(strike - atm) * 0.5),
                "ce_oi": 100000 + (strike - (atm - 100)) * 5,
                "pe_oi": 120000 + ((atm + 100) - strike) * 5,
                "ce_volume": 9000 + (strike - (atm - 100)),
                "pe_volume": 8500 + ((atm + 100) - strike),
                "ce_iv": 18.0,
                "pe_iv": 19.5,
            }
        )
    return {
        "instrument": "BANKNIFTY26MARFUT",
        "expiry": expiry,
        "pcr": pcr,
        "max_pain": atm,
        "strikes": strikes,
        "timestamp": "2024-05-08T10:10:00+05:30",
    }


class MarketSnapshotTests(unittest.TestCase):
    def test_merge_ohlc_history_dedup(self) -> None:
        supplemental = _make_ohlc("2024-05-07 09:15:00", periods=3)
        primary = _make_ohlc("2024-05-08 09:15:00", periods=3)
        # Duplicate one timestamp with primary values that should win after merge.
        duplicate = supplemental.iloc[[-1]].copy()
        duplicate["close"] = 99999.0
        primary = pd.concat([duplicate, primary], ignore_index=True)

        merged = _merge_ohlc_history(primary=primary, supplemental=supplemental)
        self.assertGreaterEqual(len(merged), 6)
        self.assertEqual(len(merged["timestamp"]), len(set(merged["timestamp"])))
        latest_dup = merged[merged["timestamp"] == duplicate["timestamp"].iloc[0]].iloc[-1]
        self.assertAlmostEqual(float(latest_dup["close"]), 99999.0, places=6)

    def test_build_market_snapshot_blocks(self) -> None:
        state = MarketSnapshotState()
        ohlc = _make_ohlc("2024-05-08 09:15:00", periods=70)
        chain = _make_chain(atm=46100, pcr=1.12, expiry="2024-05-09")
        vix = pd.DataFrame(
            {
                "trade_date": ["2024-05-07", "2024-05-08"],
                "vix_open": [13.5, 14.0],
                "vix_high": [14.1, 14.4],
                "vix_low": [13.2, 13.8],
                "vix_close": [13.8, 14.2],
            }
        )

        snap = build_market_snapshot(
            instrument="BANKNIFTY26MARFUT",
            ohlc=ohlc,
            chain=chain,
            state=state,
            vix_daily=vix,
            vix_live_current=None,
        )

        self.assertEqual(snap["schema_name"], "MarketSnapshot")
        self.assertEqual(snap["version"], "1.0")
        self.assertEqual(snap["session_context"]["snapshot_id"], snap["snapshot_id"])
        self.assertIn("futures_bar", snap)
        self.assertIn("futures_derived", snap)
        self.assertIn("opening_range", snap)
        self.assertIn("vix_context", snap)
        self.assertIn("chain_aggregates", snap)
        self.assertIn("atm_options", snap)
        self.assertIn("iv_derived", snap)
        self.assertIn("session_levels", snap)

        self.assertEqual(snap["chain_aggregates"]["atm_strike"], 46100)
        self.assertEqual(snap["atm_options"]["atm_ce_strike"], 46100)
        self.assertAlmostEqual(float(snap["atm_options"]["atm_ce_iv"]), 0.18, places=6)
        self.assertAlmostEqual(float(snap["atm_options"]["atm_pe_iv"]), 0.195, places=6)
        self.assertEqual(snap["iv_derived"]["iv_skew_dir"], "PUT_FEAR")
        self.assertIsNotNone(snap["opening_range"]["or_width"])

    def test_prev_day_session_levels_when_history_present(self) -> None:
        state = MarketSnapshotState()
        day1 = _make_ohlc("2024-05-07 09:15:00", periods=70)
        day2 = _make_ohlc("2024-05-08 09:15:00", periods=70)
        ohlc = pd.concat([day1, day2], ignore_index=True)
        chain = _make_chain(atm=46100, pcr=1.12, expiry="2024-05-09")

        snap = build_market_snapshot(
            instrument="BANKNIFTY26MARFUT",
            ohlc=ohlc,
            chain=chain,
            state=state,
            vix_daily=pd.DataFrame(),
            vix_live_current=None,
        )

        levels = snap["session_levels"]
        self.assertIsNotNone(levels["prev_day_high"])
        self.assertIsNotNone(levels["prev_day_low"])
        self.assertIsNotNone(levels["prev_day_close"])

    def test_stateful_30m_change_and_iv_percentile(self) -> None:
        state = MarketSnapshotState()

        ohlc_1 = _make_ohlc("2024-05-08 09:15:00", periods=70)
        chain_1 = _make_chain(atm=46100, pcr=1.00, expiry="2024-05-09")
        snap_1 = build_market_snapshot(
            instrument="BANKNIFTY26MARFUT",
            ohlc=ohlc_1,
            chain=chain_1,
            state=state,
            vix_daily=pd.DataFrame(),
            vix_live_current=None,
        )
        self.assertIsNone(snap_1["chain_aggregates"]["pcr_change_30m"])
        self.assertIsNone(snap_1["iv_derived"]["iv_percentile"])

        ohlc_2 = _make_ohlc("2024-05-08 09:15:00", periods=102)
        chain_2 = _make_chain(atm=46200, pcr=1.25, expiry="2024-05-09")
        # raise IV so percentile should become finite with 1 prior sample
        for strike in chain_2["strikes"]:
            if strike["strike"] == 46200:
                strike["ce_iv"] = 25.0
                strike["pe_iv"] = 24.0
        snap_2 = build_market_snapshot(
            instrument="BANKNIFTY26MARFUT",
            ohlc=ohlc_2,
            chain=chain_2,
            state=state,
            vix_daily=pd.DataFrame(),
            vix_live_current=None,
        )

        self.assertIsNotNone(snap_2["chain_aggregates"]["pcr_change_30m"])
        self.assertAlmostEqual(float(snap_2["chain_aggregates"]["pcr_change_30m"]), 0.25, places=6)
        self.assertIsNotNone(snap_2["iv_derived"]["iv_percentile"])
        self.assertGreaterEqual(float(snap_2["iv_derived"]["iv_percentile"]), 0.0)
        self.assertLessEqual(float(snap_2["iv_derived"]["iv_percentile"]), 100.0)

    def test_prev_session_chain_baseline_override(self) -> None:
        state = MarketSnapshotState()
        day1 = _make_ohlc("2024-05-07 09:15:00", periods=70)
        day2 = _make_ohlc("2024-05-08 09:15:00", periods=70)
        ohlc = pd.concat([day1, day2], ignore_index=True)
        chain = _make_chain(atm=46100, pcr=1.12, expiry="2024-05-09")
        baseline = {"trade_date": "2024-05-07", "pcr": 1.33, "max_pain": 46000}

        snap = build_market_snapshot(
            instrument="BANKNIFTY26MARFUT",
            ohlc=ohlc,
            chain=chain,
            state=state,
            vix_daily=pd.DataFrame(),
            vix_live_current=None,
            prev_session_chain_baseline=baseline,
        )

        levels = snap["session_levels"]
        self.assertAlmostEqual(float(levels["prev_day_pcr"]), 1.33, places=6)
        self.assertEqual(int(levels["prev_day_max_pain"]), 46000)


if __name__ == "__main__":
    unittest.main()
