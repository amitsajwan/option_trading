import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from ml_pipeline.fill_model import FillModelConfig
from ml_pipeline.paper_replay_evaluation import evaluate_replay, load_decisions_jsonl


def _labeled_frame() -> pd.DataFrame:
    ts = pd.to_datetime(["2023-06-15 09:15:00", "2023-06-15 09:16:00"])
    return pd.DataFrame(
        {
            "timestamp": ts,
            "trade_date": ["2023-06-15", "2023-06-15"],
            "label_horizon_minutes": [3, 3],
            "ce_entry_price": [100.0, 100.0],
            "ce_exit_price": [102.0, 101.0],
            "ce_forward_return": [0.02, 0.01],
            "ce_tp_price": [110.0, 110.0],
            "ce_sl_price": [90.0, 90.0],
            "ce_path_exit_reason": ["time_stop", "tp"],
            "ce_first_hit_offset_min": [float("nan"), 1.0],
            "pe_entry_price": [80.0, 80.0],
            "pe_exit_price": [79.0, 81.0],
            "pe_forward_return": [-0.0125, 0.0125],
            "pe_tp_price": [88.0, 88.0],
            "pe_sl_price": [72.0, 72.0],
            "pe_path_exit_reason": ["sl", "time_stop"],
            "pe_first_hit_offset_min": [0.0, float("nan")],
            "opt_0_ce_close": [100.0, 100.0],
            "opt_0_ce_high": [101.0, 102.0],
            "opt_0_ce_low": [99.0, 99.0],
            "opt_0_ce_volume": [1000.0, 900.0],
            "opt_0_pe_close": [80.0, 80.0],
            "opt_0_pe_high": [81.0, 82.0],
            "opt_0_pe_low": [79.0, 79.0],
            "opt_0_pe_volume": [800.0, 700.0],
        }
    )


class PaperReplayEvaluationTests(unittest.TestCase):
    def test_replay_alignment(self) -> None:
        decisions = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2023-06-15 09:15:00", "2023-06-15 09:16:00", "2023-06-15 09:17:00"]
                ),
                "action": ["BUY_CE", "HOLD", "BUY_PE"],
                "ce_prob": [0.7, 0.3, 0.2],
                "pe_prob": [0.2, 0.1, 0.8],
            }
        )
        profile = {
            "name": "test_profile",
            "execution_mode": "path_v2",
            "intrabar_tie_break": "sl",
            "slippage_per_trade": 0.0002,
            "forced_eod_exit_time": "15:24",
            "cost_per_trade": 0.0006,
        }
        fill_cfg = FillModelConfig(model="constant", constant_slippage=0.0)
        trades, report = evaluate_replay(
            decisions_df=decisions,
            labeled_df=_labeled_frame(),
            ce_threshold=0.5,
            pe_threshold=0.7,
            profile=profile,
            fill_model_config=fill_cfg,
        )
        self.assertEqual(report["decisions_total"], 3)
        self.assertEqual(report["buy_decisions_total"], 2)
        self.assertEqual(report["hold_decisions_total"], 1)
        self.assertEqual(report["matched_trades"], 1)
        self.assertEqual(report["unmatched_buy_decisions"], 1)
        self.assertEqual(len(trades), 1)

    def test_jsonl_timestamp_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "d.jsonl"
            rows = [
                {"timestamp": "2023-06-15T09:15:00", "action": "BUY_CE", "ce_prob": 0.7, "pe_prob": 0.2},
                {"timestamp": "bad-ts", "action": "BUY_PE", "ce_prob": 0.2, "pe_prob": 0.8},
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            df = load_decisions_jsonl(path)
            self.assertEqual(len(df), 2)
            self.assertEqual(int(df["timestamp"].isna().sum()), 1)
            self.assertEqual(df.iloc[0]["action"], "BUY_CE")


if __name__ == "__main__":
    unittest.main()
