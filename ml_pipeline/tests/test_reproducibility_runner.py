import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from ml_pipeline.reproducibility_runner import (
    _repro_summary_markdown,
    compare_artifact_sets,
    normalize_for_compare,
)


class ReproducibilityRunnerTests(unittest.TestCase):
    def test_normalize_for_compare_drops_volatile_keys(self) -> None:
        payload = {
            "created_at_utc": "2026-02-22T10:00:00Z",
            "nested": {"generated_at": "2026-02-22T10:00:01Z", "value": 10},
            "ok": True,
        }
        normalized = normalize_for_compare(payload)
        self.assertNotIn("created_at_utc", normalized)
        self.assertEqual(normalized["nested"], {"value": 10})
        self.assertTrue(normalized["ok"])

    def test_compare_artifact_sets_ignores_volatile_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run1 = root / "run1"
            run2 = root / "run2"
            (run1 / "artifacts").mkdir(parents=True, exist_ok=True)
            (run2 / "artifacts").mkdir(parents=True, exist_ok=True)

            report1 = {"created_at_utc": "x", "status": "ok", "value": 1}
            report2 = {"created_at_utc": "y", "status": "ok", "value": 1}
            (run1 / "artifacts" / "a.json").write_text(json.dumps(report1), encoding="utf-8")
            (run2 / "artifacts" / "a.json").write_text(json.dumps(report2), encoding="utf-8")

            d1 = [{"generated_at": "x", "action": "BUY_CE", "ce_prob": 0.7}]
            d2 = [{"generated_at": "y", "action": "BUY_CE", "ce_prob": 0.7}]
            (run1 / "artifacts" / "b.jsonl").write_text("\n".join(json.dumps(x) for x in d1) + "\n", encoding="utf-8")
            (run2 / "artifacts" / "b.jsonl").write_text("\n".join(json.dumps(x) for x in d2) + "\n", encoding="utf-8")

            drift1 = {"reference_events_path": "run1/a.jsonl", "current_events_path": "run1/b.jsonl", "status": "ok"}
            drift2 = {"reference_events_path": "run2/a.jsonl", "current_events_path": "run2/b.jsonl", "status": "ok"}
            (run1 / "artifacts" / "d.json").write_text(json.dumps(drift1), encoding="utf-8")
            (run2 / "artifacts" / "d.json").write_text(json.dumps(drift2), encoding="utf-8")

            df = pd.DataFrame({"x": [1, 2, 3], "y": [0.1, 0.2, 0.3]})
            df.to_parquet(run1 / "artifacts" / "c.parquet", index=False)
            df.to_parquet(run2 / "artifacts" / "c.parquet", index=False)

            cmp_report = compare_artifact_sets(
                run1_dir=run1,
                run2_dir=run2,
                artifacts=["artifacts/a.json", "artifacts/b.jsonl", "artifacts/c.parquet", "artifacts/d.json"],
            )
            self.assertEqual(cmp_report["status"], "pass")
            self.assertEqual(cmp_report["mismatch_count"], 0)

    def test_compare_artifact_sets_detects_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run1 = root / "run1"
            run2 = root / "run2"
            (run1 / "artifacts").mkdir(parents=True, exist_ok=True)
            (run2 / "artifacts").mkdir(parents=True, exist_ok=True)

            (run1 / "artifacts" / "a.json").write_text(json.dumps({"value": 1}), encoding="utf-8")
            (run2 / "artifacts" / "a.json").write_text(json.dumps({"value": 2}), encoding="utf-8")

            cmp_report = compare_artifact_sets(
                run1_dir=run1,
                run2_dir=run2,
                artifacts=["artifacts/a.json"],
            )
            self.assertEqual(cmp_report["status"], "fail")
            self.assertEqual(cmp_report["mismatch_count"], 1)

    def test_repro_summary_single_run_without_comparison(self) -> None:
        report = {
            "status": "pass",
            "created_at_utc": "2026-02-22T00:00:00+00:00",
            "base_path": "X",
            "days": ["2024-10-10"],
            "run1_dir": "run1",
            "run2_dir": None,
            "run1": {"steps": {"t01": {"seconds": 0.1}}},
            "run2": None,
            "comparison": None,
        }
        md = _repro_summary_markdown(report)
        self.assertIn("single_run=true", md)


if __name__ == "__main__":
    unittest.main()
