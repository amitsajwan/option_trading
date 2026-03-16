import unittest

import pandas as pd

from strategy_app.tools.deterministic_open_matrix import (
    _candidate_matrix,
    _gate_vs_baseline,
    _gate_vs_baseline_configured,
    _select_preferred_candidate_row,
)


class DeterministicOpenMatrixTests(unittest.TestCase):
    def test_candidate_matrix_includes_baseline_and_configs(self) -> None:
        spec = {
            "risk_profiles": [{"name": "r1", "risk_config": {"stop_loss_pct": 0.1}}],
            "regime_profiles": [{"name": "g1", "regime_config": {"trend_return_min": 0.001}}],
            "strategy_sets": [{"name": "s1", "strategies": ["ORB", "OI_BUILDUP"]}],
        }
        candidates = _candidate_matrix(spec)
        ids = [item.candidate_id for item in candidates]
        self.assertIn("baseline_default", ids)
        self.assertIn("r1_g1_s1", ids)
        candidate = [item for item in candidates if item.candidate_id == "r1_g1_s1"][0]
        enabled = candidate.metadata.get("router_config", {}).get("enabled_entry_strategies") or []
        self.assertIn("IV_FILTER", enabled)
        self.assertIn("ORB", enabled)
        self.assertIn("OI_BUILDUP", enabled)

    def test_gate_vs_baseline_returns_reasons(self) -> None:
        baseline = {"net_capital_return_pct": 1.0, "max_drawdown_pct": -0.10, "trades": 100}
        candidate = {"net_capital_return_pct": 0.5, "max_drawdown_pct": -0.20, "trades": 50}
        gates = _gate_vs_baseline(candidate, baseline)
        self.assertFalse(gates["accepted"])
        self.assertIn("return_gate_failed", gates["gate_reasons"])
        self.assertIn("drawdown_gate_failed", gates["gate_reasons"])
        self.assertIn("trade_count_gate_failed", gates["gate_reasons"])

    def test_select_preferred_candidate_row_prefers_accepted(self) -> None:
        ranked = pd.DataFrame(
            [
                {"candidate_id": "cand_1", "accepted": True, "net_capital_return_pct": 1.2},
                {"candidate_id": "baseline_default", "accepted": False, "net_capital_return_pct": 0.8},
            ]
        )
        row, reason = _select_preferred_candidate_row(ranked)
        self.assertEqual(row["candidate_id"], "cand_1")
        self.assertEqual(reason, "accepted_rank_1")

    def test_select_preferred_candidate_row_falls_back_to_baseline(self) -> None:
        ranked = pd.DataFrame(
            [
                {"candidate_id": "cand_1", "accepted": False, "net_capital_return_pct": 1.4},
                {"candidate_id": "baseline_default", "accepted": False, "net_capital_return_pct": 0.8},
            ]
        )
        row, reason = _select_preferred_candidate_row(ranked)
        self.assertEqual(row["candidate_id"], "baseline_default")
        self.assertEqual(reason, "baseline_fallback")

    def test_configured_gate_requires_positive_return(self) -> None:
        baseline = {"net_capital_return_pct": -0.02, "max_drawdown_pct": -0.10, "trades": 100}
        candidate = {"net_capital_return_pct": -0.01, "max_drawdown_pct": -0.09, "trades": 90}
        gates = _gate_vs_baseline_configured(
            candidate,
            baseline,
            require_positive_return=True,
            min_outperformance_pct=0.0,
        )
        self.assertFalse(gates["accepted"])
        self.assertFalse(gates["positive_return_gate"])
        self.assertIn("positive_return_gate_failed", gates["gate_reasons"])


if __name__ == "__main__":
    unittest.main()
