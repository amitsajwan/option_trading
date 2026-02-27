import os
import unittest

from ml_pipeline.dataset_builder import build_canonical_dataset
from ml_pipeline.schema_validator import resolve_archive_base


class DatasetBuilderIntegrationTests(unittest.TestCase):
    def test_real_archive_row_count_and_alignment(self) -> None:
        base = resolve_archive_base(explicit_base=os.getenv("LOCAL_HISTORICAL_BASE"))
        if base is None:
            self.skipTest("Archive base path not found")
        days = ["2023-06-15", "2024-10-10"]
        panel = build_canonical_dataset(base_path=base, days=days)
        self.assertGreaterEqual(len(panel), 700)
        self.assertEqual(panel["timestamp"].nunique(), len(panel))
        self.assertTrue((panel["fut_close"].notna()).all())
        self.assertGreater(panel["spot_close"].notna().sum(), 650)


if __name__ == "__main__":
    unittest.main()

