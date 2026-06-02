import unittest

from market_data_dashboard.routes.ops_routes import _summarize_trades


class OpsSummaryTests(unittest.TestCase):
    def test_empty(self) -> None:
        s = _summarize_trades([])
        self.assertEqual(s["trade_count"], 0)
        self.assertEqual(s["capture_ratio"], 0.0)

    def test_capture_is_aggregate_not_mean_of_ratios(self) -> None:
        """Regression for the '-85% MFE capture' screenshot artifact.

        A single trade with a tiny MFE and a loss (pnl -5.10% on mfe +0.86%)
        has a per-trade ratio of ~-5.93. The old mean-of-ratios form let that
        one trade swamp the average into a nonsense ~-85%. The aggregate
        Σpnl/Σmfe stays bounded by the favorable move actually available.
        """
        trades = [
            {"pnl_pct": 0.0085, "mfe_pct": 0.0143, "prem_in": 1046},
            {"pnl_pct": 0.0062, "mfe_pct": 0.0102, "prem_in": 955},
            {"pnl_pct": -0.0510, "mfe_pct": 0.0086, "prem_in": 922},   # the swamper
            {"pnl_pct": -0.0379, "mfe_pct": 0.0476, "prem_in": 962},
            {"pnl_pct": 0.0009, "mfe_pct": 0.0148, "prem_in": 1039},
            {"pnl_pct": 0.0291, "mfe_pct": 0.0476, "prem_in": 950},
            {"pnl_pct": -0.0085, "mfe_pct": 0.0103, "prem_in": 965},
        ]
        s = _summarize_trades(trades)

        cap_num = sum(t["pnl_pct"] for t in trades)
        cap_den = sum(t["mfe_pct"] for t in trades)
        self.assertAlmostEqual(s["capture_ratio"], cap_num / cap_den, places=6)

        # The old mean-of-ratios form produced < -0.5 here; the aggregate must not.
        mean_of_ratios = sum(t["pnl_pct"] / t["mfe_pct"] for t in trades) / len(trades)
        self.assertLess(mean_of_ratios, -0.5)
        self.assertGreater(s["capture_ratio"], -0.5)

    def test_zero_mfe_trades_excluded_from_capture(self) -> None:
        trades = [
            {"pnl_pct": -0.0257, "mfe_pct": 0.0, "prem_in": 1056},  # mfe 0 → ignored
            {"pnl_pct": 0.0588, "mfe_pct": 0.0588, "prem_in": 1138},
        ]
        s = _summarize_trades(trades)
        self.assertAlmostEqual(s["capture_ratio"], 1.0, places=6)
        self.assertEqual(s["win_count"], 1)
        self.assertEqual(s["trade_count"], 2)


if __name__ == "__main__":
    unittest.main()
