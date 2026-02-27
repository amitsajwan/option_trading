import os
import unittest

from ml_pipeline.dataset_builder import build_canonical_dataset
from ml_pipeline.feature.engineering import build_feature_table
from ml_pipeline.label_engine import EffectiveLabelConfig, build_labeled_dataset
from ml_pipeline.schema_validator import resolve_archive_base


class LabelEngineIntegrationTests(unittest.TestCase):
    def test_real_archive_label_generation(self) -> None:
        base = resolve_archive_base(explicit_base=os.getenv("LOCAL_HISTORICAL_BASE"))
        if base is None:
            self.skipTest("Archive base path not found")

        day = ["2023-06-15"]
        panel = build_canonical_dataset(base_path=base, days=day)
        features = build_feature_table(panel)
        cfg = EffectiveLabelConfig(
            horizon_minutes=3,
            return_threshold=0.002,
            use_excursion_gate=False,
            min_favorable_excursion=0.002,
            max_adverse_excursion=0.001,
        )
        labeled = build_labeled_dataset(features=features, base_path=base, cfg=cfg)

        self.assertEqual(len(labeled), len(features))
        self.assertGreater(int((labeled["ce_label_valid"] == 1.0).sum()), 300)
        self.assertGreater(int((labeled["pe_label_valid"] == 1.0).sum()), 300)
        self.assertGreaterEqual(float(labeled["ce_label"].fillna(0.0).mean()), 0.0)
        self.assertLessEqual(float(labeled["ce_label"].fillna(0.0).mean()), 1.0)
        self.assertGreaterEqual(float(labeled["pe_label"].fillna(0.0).mean()), 0.0)
        self.assertLessEqual(float(labeled["pe_label"].fillna(0.0).mean()), 1.0)
        self.assertIn("ce_path_exit_reason", labeled.columns)
        self.assertIn("pe_path_exit_reason", labeled.columns)
        ce_reasons = set(str(x) for x in labeled["ce_path_exit_reason"].dropna().unique().tolist())
        pe_reasons = set(str(x) for x in labeled["pe_path_exit_reason"].dropna().unique().tolist())
        allowed = {"tp", "sl", "tp_sl_same_bar", "time_stop", "invalid"}
        self.assertTrue(ce_reasons.issubset(allowed))
        self.assertTrue(pe_reasons.issubset(allowed))


if __name__ == "__main__":
    unittest.main()

