import tempfile
import unittest
from pathlib import Path

import pandas as pd

from strategy_app.tools.compare_holdout_registry import compare_holdout_registries


class CompareHoldoutRegistryTests(unittest.TestCase):
    def test_compare_passes_medium_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            baseline = root / "baseline.csv"
            candidate = root / "candidate.csv"
            out = root / "out"
            pd.DataFrame(
                [
                    {
                        "candidate_id": "base",
                        "net_capital_return_pct": 0.0100,
                        "max_drawdown_pct": -0.0200,
                        "trades": 100,
                        "accepted": True,
                    }
                ]
            ).to_csv(baseline, index=False)
            pd.DataFrame(
                [
                    {
                        "candidate_id": "cand",
                        "net_capital_return_pct": 0.0095,
                        "max_drawdown_pct": -0.0210,
                        "trades": 85,
                        "accepted": True,
                    }
                ]
            ).to_csv(candidate, index=False)

            summary = compare_holdout_registries(
                baseline_holdout_registry=baseline,
                candidate_holdout_registry=candidate,
                output_dir=out,
            )
            self.assertTrue(summary["gate_results"]["passed"])

    def test_compare_fails_when_drawdown_and_trades_worse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            baseline = root / "baseline.csv"
            candidate = root / "candidate.csv"
            out = root / "out"
            pd.DataFrame(
                [
                    {
                        "candidate_id": "base",
                        "net_capital_return_pct": 0.0100,
                        "max_drawdown_pct": -0.0200,
                        "trades": 100,
                        "accepted": True,
                    }
                ]
            ).to_csv(baseline, index=False)
            pd.DataFrame(
                [
                    {
                        "candidate_id": "cand",
                        "net_capital_return_pct": 0.0060,
                        "max_drawdown_pct": -0.0300,
                        "trades": 70,
                        "accepted": True,
                    }
                ]
            ).to_csv(candidate, index=False)

            summary = compare_holdout_registries(
                baseline_holdout_registry=baseline,
                candidate_holdout_registry=candidate,
                output_dir=out,
            )
            self.assertFalse(summary["gate_results"]["passed"])
            self.assertIn("drawdown_gate_failed", summary["gate_results"]["reasons"])
            self.assertIn("trade_count_gate_failed", summary["gate_results"]["reasons"])


if __name__ == "__main__":
    unittest.main()
