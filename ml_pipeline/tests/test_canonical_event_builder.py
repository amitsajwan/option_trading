import unittest

import numpy as np
import pandas as pd

from ml_pipeline.canonical_event_builder import (
    apply_option_change_features,
    build_canonical_event_from_ohlc_and_chain,
    build_vix_snapshot_for_trade_date,
    chain_from_options_minute,
)
from ml_pipeline.live_inference_adapter import build_live_canonical_event


class CanonicalEventBuilderTests(unittest.TestCase):
    def _sample_ohlc(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-06-07 09:15:00", "2024-06-07 09:16:00"]),
                "open": [48000.0, 48020.0],
                "high": [48030.0, 48050.0],
                "low": [47980.0, 48010.0],
                "close": [48020.0, 48040.0],
                "volume": [1200.0, 1400.0],
                "oi": [1_000_000.0, 1_001_000.0],
            }
        )

    def _sample_chain(self) -> dict:
        return {
            "expiry": "20240627",
            "strikes": [
                {"strike": 47900, "ce_ltp": 210.0, "pe_ltp": 80.0, "ce_oi": 1000, "pe_oi": 1200, "ce_volume": 400, "pe_volume": 500},
                {"strike": 48000, "ce_ltp": 160.0, "pe_ltp": 110.0, "ce_oi": 1500, "pe_oi": 1800, "ce_volume": 600, "pe_volume": 650},
                {"strike": 48100, "ce_ltp": 120.0, "pe_ltp": 150.0, "ce_oi": 1300, "pe_oi": 1400, "ce_volume": 550, "pe_volume": 580},
            ],
            "pcr": 1.1,
        }

    def test_shared_builder_emits_expected_columns(self) -> None:
        event = build_canonical_event_from_ohlc_and_chain(
            ohlc=self._sample_ohlc(),
            chain=self._sample_chain(),
            vix_snapshot={"vix_prev_close": 13.5, "is_high_vix_day": 0.0},
        )
        required = [
            "timestamp",
            "trade_date",
            "fut_close",
            "pcr_oi",
            "atm_strike",
            "opt_0_ce_close",
            "opt_0_pe_close",
            "vix_prev_close",
            "is_high_vix_day",
        ]
        for col in required:
            self.assertIn(col, event)
        self.assertTrue(np.isfinite(float(event["fut_close"])))
        self.assertTrue(np.isfinite(float(event["atm_strike"])))

    def test_live_wrapper_parity_with_shared_builder(self) -> None:
        ohlc = self._sample_ohlc()
        chain = self._sample_chain()
        shared = build_canonical_event_from_ohlc_and_chain(ohlc=ohlc, chain=chain, vix_snapshot=None)
        live = build_live_canonical_event(
            ohlc=ohlc,
            chain=chain,
            options_extractor=lambda c, fut_price: {},
            rsi_fn=lambda s, p: s,
            atr_fn=lambda d, p: d["close"],
            vwap_fn=lambda d: d["close"],
            vix_snapshot=None,
        )
        self.assertEqual(set(shared.keys()), set(live.keys()))
        for key in ("fut_close", "pcr_oi", "atm_strike", "ce_pe_oi_diff", "ce_pe_volume_diff"):
            self.assertAlmostEqual(float(shared[key]), float(live[key]), places=8)

    def test_option_change_features_stateful(self) -> None:
        row1 = {"trade_date": "2024-06-07", "opt_0_ce_close": 100.0, "opt_0_pe_close": 120.0, "opt_0_ce_oi": 1000.0, "opt_0_pe_oi": 1500.0}
        state = apply_option_change_features(
            row1,
            prev_trade_date=None,
            prev_opt0_ce_close=None,
            prev_opt0_pe_close=None,
            prev_opt0_total_oi=None,
        )
        self.assertTrue(np.isnan(float(row1["atm_call_return_1m"])))
        row2 = {"trade_date": "2024-06-07", "opt_0_ce_close": 110.0, "opt_0_pe_close": 114.0, "opt_0_ce_oi": 1020.0, "opt_0_pe_oi": 1515.0}
        apply_option_change_features(
            row2,
            prev_trade_date=state[0],
            prev_opt0_ce_close=state[1],
            prev_opt0_pe_close=state[2],
            prev_opt0_total_oi=state[3],
        )
        self.assertAlmostEqual(float(row2["atm_call_return_1m"]), 0.1, places=8)

    def test_chain_from_options_minute(self) -> None:
        options = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-06-07 09:16:00"] * 4),
                "strike": [48000, 48000, 48100, 48100],
                "option_type": ["CE", "PE", "CE", "PE"],
                "close": [160.0, 110.0, 120.0, 150.0],
                "oi": [1500, 1800, 1300, 1400],
                "volume": [600, 650, 550, 580],
                "expiry_code": ["20240627"] * 4,
            }
        )
        chain = chain_from_options_minute(options)
        self.assertIn("strikes", chain)
        self.assertEqual(len(chain["strikes"]), 2)
        self.assertEqual(chain["expiry"], "20240627")

    def test_vix_snapshot_previous_day_alignment(self) -> None:
        vix = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-06-05", "2024-06-06"]),
                "vix_close": [15.0, 16.0],
            }
        )
        snap = build_vix_snapshot_for_trade_date(vix_daily=vix, trade_date="2024-06-07")
        self.assertAlmostEqual(float(snap["vix_prev_close"]), 16.0, places=8)


if __name__ == "__main__":
    unittest.main()
