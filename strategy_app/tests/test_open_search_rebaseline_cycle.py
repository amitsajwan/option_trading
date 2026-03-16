import tempfile
import unittest
from pathlib import Path

import pandas as pd

from strategy_app.tools.open_search_rebaseline_cycle import _choose_finalists


class OpenSearchRebaselineCycleTests(unittest.TestCase):
    def test_choose_finalists_prefers_gate_champions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fallback = root / "evaluation_registry.csv"
            pd.DataFrame(
                [
                    {"experiment_id": "fallback_1", "ml_capital_return_pct": 0.02, "ml_max_drawdown_pct": -0.05, "ml_profit_factor": 1.1, "ml_trades": 10},
                    {"experiment_id": "fallback_2", "ml_capital_return_pct": 0.01, "ml_max_drawdown_pct": -0.04, "ml_profit_factor": 1.0, "ml_trades": 8},
                ]
            ).to_csv(fallback, index=False)
            payload = {"champions": [{"experiment_id": "hard_gate_1"}, {"experiment_id": "hard_gate_2"}]}
            finalists = _choose_finalists(payload, fallback, max_finalists=2)
            self.assertEqual(finalists, ["hard_gate_1", "hard_gate_2"])

    def test_choose_finalists_uses_diversified_rejected_before_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fallback = root / "evaluation_registry.csv"
            pd.DataFrame(
                [
                    {"experiment_id": "fallback_1", "ml_capital_return_pct": 0.10, "ml_max_drawdown_pct": -0.06, "ml_profit_factor": 1.4, "ml_trades": 25},
                ]
            ).to_csv(fallback, index=False)
            payload = {
                "champions": [],
                "rejected_candidates": [
                    {
                        "experiment_id": "concentrated_1",
                        "ml_capital_return_pct": 0.12,
                        "ml_max_drawdown_pct": -0.07,
                        "ml_profit_factor": 1.5,
                        "ml_trades": 30,
                        "min_trades_gate": True,
                        "max_drawdown_gate": True,
                        "drawdown_gate": True,
                        "trade_count_gate": True,
                        "strategy_diversification_gate": False,
                        "return_gate": True,
                        "positive_return_gate": True,
                    },
                    {
                        "experiment_id": "diversified_1",
                        "ml_capital_return_pct": 0.09,
                        "ml_max_drawdown_pct": -0.05,
                        "ml_profit_factor": 1.3,
                        "ml_trades": 28,
                        "min_trades_gate": True,
                        "max_drawdown_gate": True,
                        "drawdown_gate": True,
                        "trade_count_gate": True,
                        "strategy_diversification_gate": True,
                        "return_gate": True,
                        "positive_return_gate": True,
                    },
                ],
            }
            finalists = _choose_finalists(payload, fallback, max_finalists=1)
            self.assertEqual(finalists, ["diversified_1"])

    def test_choose_finalists_respects_positive_return_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fallback = root / "evaluation_registry.csv"
            pd.DataFrame(
                [
                    {"experiment_id": "fallback_neg", "ml_capital_return_pct": -0.01, "ml_max_drawdown_pct": -0.05, "ml_profit_factor": 1.0, "ml_trades": 10},
                ]
            ).to_csv(fallback, index=False)
            finalists = _choose_finalists(
                {"champions": [], "rejected_candidates": []},
                fallback,
                max_finalists=1,
                require_positive_return=True,
            )
            self.assertEqual(finalists, [])


if __name__ == "__main__":
    unittest.main()
