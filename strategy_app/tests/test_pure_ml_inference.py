import json
import tempfile
import unittest
from pathlib import Path

from strategy_app.engines.pure_ml_inference import infer_action, load_runtime_controls


class PureMLInferenceTests(unittest.TestCase):
    def test_low_edge_conflict_returns_hold_reason(self) -> None:
        action, reason = infer_action(
            ce_prob=0.65,
            pe_prob=0.62,
            ce_threshold=0.60,
            pe_threshold=0.60,
            min_edge=0.15,
        )
        self.assertEqual(action, "HOLD")
        self.assertEqual(reason, "low_edge_conflict")

    def test_load_runtime_controls_defaults_block_expiry_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "thresholds.json"
            path.write_text(json.dumps({"ce_threshold": 0.6, "pe_threshold": 0.6}), encoding="utf-8")

            controls = load_runtime_controls(path)

            self.assertFalse(controls.block_expiry)

    def test_load_runtime_controls_reads_runtime_block_expiry_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "thresholds.json"
            path.write_text(
                json.dumps(
                    {
                        "ce_threshold": 0.6,
                        "pe_threshold": 0.6,
                        "runtime": {"block_expiry": True},
                    }
                ),
                encoding="utf-8",
            )

            controls = load_runtime_controls(path)

            self.assertTrue(controls.block_expiry)


if __name__ == "__main__":
    unittest.main()
