import tempfile
import unittest
from pathlib import Path

import pandas as pd

from ml_pipeline.eda.stage import run_eda_stage
from ml_pipeline.live_inference_adapter import build_live_canonical_event


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


class EdaLiveParityIntegrationTests(unittest.TestCase):
    def test_eda_canonical_events_match_live_builder_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            day = "2024-06-07"
            yyyy = "2024"
            mm = "6"
            ddmmyyyy = "07_06_2024"

            _write_csv(
                base / "banknifty_fut" / yyyy / mm / f"banknifty_fut_{ddmmyyyy}.csv",
                [
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY-I",
                        "open": 48000,
                        "high": 48030,
                        "low": 47980,
                        "close": 48020,
                        "oi": 1000000,
                        "volume": 1200,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY-I",
                        "open": 48020,
                        "high": 48050,
                        "low": 48010,
                        "close": 48040,
                        "oi": 1001000,
                        "volume": 1400,
                    },
                ],
            )

            _write_csv(
                base / "banknifty_spot" / yyyy / mm / f"banknifty_spot{ddmmyyyy}.csv",
                [
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY",
                        "open": 47990,
                        "high": 48020,
                        "low": 47970,
                        "close": 48010,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY",
                        "open": 48010,
                        "high": 48040,
                        "low": 48000,
                        "close": 48030,
                    },
                ],
            )

            _write_csv(
                base / "banknifty_options" / yyyy / mm / f"banknifty_options_{ddmmyyyy}.csv",
                [
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2447900CE",
                        "open": 210,
                        "high": 212,
                        "low": 208,
                        "close": 211,
                        "oi": 1000,
                        "volume": 400,
                    },
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2447900PE",
                        "open": 80,
                        "high": 82,
                        "low": 78,
                        "close": 81,
                        "oi": 1200,
                        "volume": 500,
                    },
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2448000CE",
                        "open": 160,
                        "high": 162,
                        "low": 158,
                        "close": 161,
                        "oi": 1500,
                        "volume": 600,
                    },
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2448000PE",
                        "open": 110,
                        "high": 112,
                        "low": 108,
                        "close": 111,
                        "oi": 1800,
                        "volume": 650,
                    },
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2448100CE",
                        "open": 120,
                        "high": 122,
                        "low": 118,
                        "close": 121,
                        "oi": 1300,
                        "volume": 550,
                    },
                    {
                        "date": day,
                        "time": "09:15:00",
                        "symbol": "BANKNIFTY27JUN2448100PE",
                        "open": 150,
                        "high": 152,
                        "low": 148,
                        "close": 151,
                        "oi": 1400,
                        "volume": 580,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2447900CE",
                        "open": 220,
                        "high": 222,
                        "low": 218,
                        "close": 221,
                        "oi": 1010,
                        "volume": 410,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2447900PE",
                        "open": 70,
                        "high": 72,
                        "low": 68,
                        "close": 71,
                        "oi": 1210,
                        "volume": 510,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2448000CE",
                        "open": 170,
                        "high": 172,
                        "low": 168,
                        "close": 171,
                        "oi": 1510,
                        "volume": 610,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2448000PE",
                        "open": 100,
                        "high": 102,
                        "low": 98,
                        "close": 101,
                        "oi": 1810,
                        "volume": 660,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2448100CE",
                        "open": 130,
                        "high": 132,
                        "low": 128,
                        "close": 131,
                        "oi": 1310,
                        "volume": 560,
                    },
                    {
                        "date": day,
                        "time": "09:16:00",
                        "symbol": "BANKNIFTY27JUN2448100PE",
                        "open": 140,
                        "high": 142,
                        "low": 138,
                        "close": 141,
                        "oi": 1410,
                        "volume": 590,
                    },
                ],
            )

            summary = run_eda_stage(
                base_path=str(base),
                days=day,
                max_days=1,
                vix_path=None,
                out_dir=base / "out",
                train_ratio=0.7,
                valid_ratio=0.15,
            )
            self.assertEqual(summary["selected_days_total"], 1)

            events = pd.read_parquet(base / "out" / "canonical_events.parquet")
            self.assertGreaterEqual(len(events), 2)
            latest = events.iloc[-1].to_dict()
            self.assertIn("ret_1m", latest)
            self.assertIn("atm_call_return_1m", latest)
            self.assertIn("pcr_oi", latest)
            self.assertTrue(pd.notna(latest["fut_close"]))

            ohlc = pd.DataFrame(
                {
                    "timestamp": pd.to_datetime([f"{day} 09:15:00", f"{day} 09:16:00"]),
                    "open": [48000.0, 48020.0],
                    "high": [48030.0, 48050.0],
                    "low": [47980.0, 48010.0],
                    "close": [48020.0, 48040.0],
                    "volume": [1200.0, 1400.0],
                    "oi": [1000000.0, 1001000.0],
                }
            )
            chain = {
                "expiry": "20240627",
                "strikes": [
                    {"strike": 47900.0, "ce_ltp": 221.0, "pe_ltp": 71.0, "ce_oi": 1010.0, "pe_oi": 1210.0, "ce_volume": 410.0, "pe_volume": 510.0},
                    {"strike": 48000.0, "ce_ltp": 171.0, "pe_ltp": 101.0, "ce_oi": 1510.0, "pe_oi": 1810.0, "ce_volume": 610.0, "pe_volume": 660.0},
                    {"strike": 48100.0, "ce_ltp": 131.0, "pe_ltp": 141.0, "ce_oi": 1310.0, "pe_oi": 1410.0, "ce_volume": 560.0, "pe_volume": 590.0},
                ],
                "pcr": 1.0,
            }
            live_event = build_live_canonical_event(
                ohlc=ohlc,
                chain=chain,
                options_extractor=lambda c, fut_price: {},
                rsi_fn=lambda s, p: s,
                atr_fn=lambda d, p: d["close"],
                vwap_fn=lambda d: d["close"],
                vix_snapshot=None,
            )
            self.assertAlmostEqual(float(latest["fut_close"]), float(live_event["fut_close"]), places=8)
            self.assertAlmostEqual(float(latest["pcr_oi"]), float(live_event["pcr_oi"]), places=8)
            self.assertAlmostEqual(float(latest["atm_strike"]), float(live_event["atm_strike"]), places=8)


if __name__ == "__main__":
    unittest.main()
