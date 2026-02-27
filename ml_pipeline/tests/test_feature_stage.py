import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from ml_pipeline.feature.stage import run_feature_stage


class FeatureStageTests(unittest.TestCase):
    def test_run_feature_stage_outputs_profiled_splits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            canonical = root / "canonical_events.parquet"
            split = root / "split.json"
            out_root = root / "out"

            frame = pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2024-10-30 09:15:00", "2024-10-31 09:15:00"]),
                    "trade_date": ["2024-10-30", "2024-10-31"],
                    "ret_1m": [0.01, 0.02],
                    "ret_5m": [0.03, 0.04],
                    "rsi_14": [55.0, 56.0],
                    "fut_close": [100.0, 101.0],
                }
            )
            frame.to_parquet(canonical, index=False)
            split_payload = {
                "days": {"train": ["2024-10-30"], "valid": [], "eval": ["2024-10-31"]},
            }
            split.write_text(json.dumps(split_payload), encoding="utf-8")

            summary = run_feature_stage(
                canonical_events_path=canonical,
                split_report_path=split,
                profile="core_v1",
                out_root=out_root,
            )
            self.assertEqual(summary["rows"]["train"], 1)
            self.assertEqual(summary["rows"]["eval"], 1)
            self.assertTrue((out_root / "core_v1" / "train.parquet").exists())
            self.assertTrue((out_root / "core_v1" / "lineage.json").exists())


if __name__ == "__main__":
    unittest.main()

