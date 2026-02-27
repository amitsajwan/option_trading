import unittest

import numpy as np
import pandas as pd

from ml_pipeline.label_engine import EffectiveLabelConfig, label_day


class LabelEngineUnitTests(unittest.TestCase):
    def test_label_day_uses_fixed_symbol_not_shifted_dynamic_atm(self) -> None:
        features = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
                "trade_date": ["2023-06-15", "2023-06-15"],
                "expiry_code": ["15JUN23", "15JUN23"],
                "atm_strike": [44100, 44200],
            }
        )
        options = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2023-06-15 09:16:00",
                        "2023-06-15 09:17:00",
                        "2023-06-15 09:18:00",
                        "2023-06-15 09:16:00",
                        "2023-06-15 09:17:00",
                        "2023-06-15 09:18:00",
                    ]
                ),
                "symbol": [
                    "BANKNIFTY15JUN2344100CE",
                    "BANKNIFTY15JUN2344100CE",
                    "BANKNIFTY15JUN2344100CE",
                    "BANKNIFTY15JUN2344200CE",
                    "BANKNIFTY15JUN2344200CE",
                    "BANKNIFTY15JUN2344200CE",
                ],
                "open": [100.0, 101.0, 102.0, 250.0, 255.0, 260.0],
                "high": [102.0, 103.0, 111.0, 252.0, 258.0, 265.0],
                "low": [99.0, 100.0, 101.0, 249.0, 251.0, 258.0],
                "close": [101.0, 102.0, 110.0, 252.0, 257.0, 262.0],
                "oi": [1000, 1000, 1000, 900, 900, 900],
                "volume": [100, 100, 100, 90, 90, 90],
                "strike": [44100, 44100, 44100, 44200, 44200, 44200],
                "option_type": ["CE", "CE", "CE", "CE", "CE", "CE"],
                "expiry_code": ["15JUN23"] * 6,
            }
        )
        # Add PE rows so the function can populate both sides without KeyError.
        pe_rows = options.copy()
        pe_rows["symbol"] = pe_rows["symbol"].str.replace("CE", "PE", regex=False)
        pe_rows["option_type"] = "PE"
        pe_rows["open"] = pe_rows["open"] * 0.8
        pe_rows["high"] = pe_rows["high"] * 0.8
        pe_rows["low"] = pe_rows["low"] * 0.8
        pe_rows["close"] = pe_rows["close"] * 0.8
        options_all = pd.concat([options, pe_rows], ignore_index=True)

        cfg = EffectiveLabelConfig(
            horizon_minutes=3,
            return_threshold=0.02,
            use_excursion_gate=False,
            min_favorable_excursion=0.02,
            max_adverse_excursion=0.01,
        )
        labeled = label_day(features, options_all, cfg)
        first = labeled.iloc[0]
        self.assertEqual(first["ce_symbol"], "BANKNIFTY15JUN2344100CE")
        # Entry at 09:16 open=100, exit at 09:18 close=110 => 10% return.
        self.assertAlmostEqual(first["ce_forward_return"], 0.10, places=8)
        self.assertEqual(first["ce_label"], 1.0)

    def test_excursion_gate_blocks_positive_return_when_mae_too_low(self) -> None:
        features = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00"]),
                "trade_date": ["2023-06-15"],
                "expiry_code": ["15JUN23"],
                "atm_strike": [44100],
            }
        )
        options = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:16:00", "2023-06-15 09:17:00", "2023-06-15 09:18:00"]),
                "symbol": ["BANKNIFTY15JUN2344100CE"] * 3,
                "open": [100.0, 100.0, 100.0],
                "high": [102.0, 103.0, 106.0],
                "low": [90.0, 89.0, 95.0],  # Deep adverse excursion
                "close": [100.0, 101.0, 104.0],
                "oi": [1000, 1000, 1000],
                "volume": [100, 100, 100],
                "strike": [44100, 44100, 44100],
                "option_type": ["CE", "CE", "CE"],
                "expiry_code": ["15JUN23"] * 3,
            }
        )
        pe = options.copy()
        pe["symbol"] = "BANKNIFTY15JUN2344100PE"
        pe["option_type"] = "PE"
        options_all = pd.concat([options, pe], ignore_index=True)
        cfg = EffectiveLabelConfig(
            horizon_minutes=3,
            return_threshold=0.02,
            use_excursion_gate=True,
            min_favorable_excursion=0.02,
            max_adverse_excursion=0.05,
        )
        labeled = label_day(features, options_all, cfg)
        row = labeled.iloc[0]
        self.assertTrue(np.isfinite(row["ce_forward_return"]))
        self.assertGreater(row["ce_forward_return"], 0.02)
        # MAE should violate gate, forcing negative label.
        self.assertEqual(row["ce_label"], 0.0)

    def test_path_outcome_tp_first_and_time_stop(self) -> None:
        features = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"]),
                "trade_date": ["2023-06-15", "2023-06-15"],
                "expiry_code": ["15JUN23", "15JUN23"],
                "atm_strike": [44100, 44100],
            }
        )
        # Decision at 09:15 => entry 09:16, exit 09:18.
        # 09:17 bar hits TP (high >= 110 for entry 100 and tp=10%).
        options = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2023-06-15 09:16:00",
                        "2023-06-15 09:17:00",
                        "2023-06-15 09:18:00",
                        "2023-06-15 09:19:00",
                    ]
                ),
                "symbol": ["BANKNIFTY15JUN2344100CE"] * 4,
                "open": [100.0, 101.0, 102.0, 103.0],
                "high": [105.0, 111.0, 106.0, 108.0],
                "low": [99.0, 100.0, 101.0, 100.0],
                "close": [101.0, 110.0, 104.0, 106.0],
                "oi": [1000, 1000, 1000, 1000],
                "volume": [100, 100, 100, 100],
                "strike": [44100, 44100, 44100, 44100],
                "option_type": ["CE", "CE", "CE", "CE"],
                "expiry_code": ["15JUN23"] * 4,
            }
        )
        pe = options.copy()
        pe["symbol"] = "BANKNIFTY15JUN2344100PE"
        pe["option_type"] = "PE"
        options_all = pd.concat([options, pe], ignore_index=True)

        cfg = EffectiveLabelConfig(
            horizon_minutes=3,
            return_threshold=0.02,
            use_excursion_gate=False,
            min_favorable_excursion=0.02,
            max_adverse_excursion=0.01,
            stop_loss_pct=0.05,
            take_profit_pct=0.10,
            allow_hold_extension=True,
            extension_trigger_profit_pct=0.01,
        )
        labeled = label_day(features, options_all, cfg)
        first = labeled.iloc[0]
        self.assertEqual(first["ce_path_exit_reason"], "tp")
        self.assertEqual(first["ce_tp_hit"], 1.0)
        self.assertEqual(first["ce_sl_hit"], 0.0)
        self.assertEqual(first["ce_time_stop_exit"], 0.0)

        # Decision at 09:16 => entry 09:17, exit 09:19; no TP/SL hit in-window.
        second = labeled.iloc[1]
        self.assertEqual(second["ce_path_exit_reason"], "time_stop")
        self.assertEqual(second["ce_time_stop_exit"], 1.0)
        self.assertEqual(second["ce_hold_extension_eligible"], 1.0)

    def test_path_labels_no_lookahead_beyond_horizon(self) -> None:
        features = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2023-06-15 09:15:00"]),
                "trade_date": ["2023-06-15"],
                "expiry_code": ["15JUN23"],
                "atm_strike": [44100],
            }
        )
        base_options = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2023-06-15 09:16:00",
                        "2023-06-15 09:17:00",
                        "2023-06-15 09:18:00",
                        "2023-06-15 09:19:00",
                    ]
                ),
                "symbol": ["BANKNIFTY15JUN2344100CE"] * 4,
                "open": [100.0, 101.0, 102.0, 103.0],
                "high": [104.0, 105.0, 106.0, 200.0],
                "low": [99.0, 99.0, 99.0, 1.0],
                "close": [101.0, 102.0, 103.0, 50.0],
                "oi": [1000, 1000, 1000, 1000],
                "volume": [100, 100, 100, 100],
                "strike": [44100, 44100, 44100, 44100],
                "option_type": ["CE", "CE", "CE", "CE"],
                "expiry_code": ["15JUN23"] * 4,
            }
        )
        pe = base_options.copy()
        pe["symbol"] = "BANKNIFTY15JUN2344100PE"
        pe["option_type"] = "PE"
        options_a = pd.concat([base_options, pe], ignore_index=True)

        options_b = options_a.copy()
        # mutate only post-horizon bar (09:19) aggressively
        mask_post = options_b["timestamp"] == pd.Timestamp("2023-06-15 09:19:00")
        options_b.loc[mask_post, "high"] = 1000.0
        options_b.loc[mask_post, "low"] = 0.1
        options_b.loc[mask_post, "close"] = 0.5

        cfg = EffectiveLabelConfig(
            horizon_minutes=3,
            return_threshold=0.02,
            use_excursion_gate=False,
            min_favorable_excursion=0.02,
            max_adverse_excursion=0.01,
            stop_loss_pct=0.10,
            take_profit_pct=0.15,
        )
        labeled_a = label_day(features, options_a, cfg).iloc[0]
        labeled_b = label_day(features, options_b, cfg).iloc[0]
        self.assertEqual(labeled_a["ce_path_exit_reason"], labeled_b["ce_path_exit_reason"])
        self.assertAlmostEqual(float(labeled_a["ce_forward_return"]), float(labeled_b["ce_forward_return"]), places=10)
        self.assertAlmostEqual(float(labeled_a["ce_mfe"]), float(labeled_b["ce_mfe"]), places=10)
        self.assertAlmostEqual(float(labeled_a["ce_mae"]), float(labeled_b["ce_mae"]), places=10)


if __name__ == "__main__":
    unittest.main()
