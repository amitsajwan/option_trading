import unittest
from datetime import datetime

from market_data_dashboard.strategy_evaluation_service import StrategyEvaluationService, _iso_or_none


class StrategyEvaluationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StrategyEvaluationService()

    def test_apply_capital_metrics_and_equity_use_capital_weighting(self) -> None:
        trades = [
            {
                "position_id": "p1",
                "trade_date_ist": "2024-01-01",
                "exit_time": "2024-01-01T10:00:00Z",
                "exit_dt": None,
                "entry_premium": 100.0,
                "lots": 1,
                "lot_size": 15.0,
                "pnl_pct_net": 0.10,
            },
            {
                "position_id": "p2",
                "trade_date_ist": "2024-01-01",
                "exit_time": "2024-01-01T10:05:00Z",
                "exit_dt": None,
                "entry_premium": 200.0,
                "lots": 1,
                "lot_size": 15.0,
                "pnl_pct_net": -0.05,
            },
        ]

        enriched = self.service._apply_capital_metrics(trades, initial_capital=1000.0)

        self.assertAlmostEqual(enriched[0]["capital_at_risk"], 1500.0, places=6)
        self.assertAlmostEqual(enriched[0]["capital_pnl_amount"], 150.0, places=6)
        self.assertAlmostEqual(enriched[0]["capital_pnl_pct"], 0.15, places=6)
        self.assertAlmostEqual(enriched[1]["capital_pnl_amount"], -150.0, places=6)
        self.assertAlmostEqual(enriched[1]["capital_pnl_pct"], -0.15, places=6)

        summary = self.service._summarize_trades(enriched)
        self.assertAlmostEqual(summary["avg_capital_pnl_pct"], 0.0, places=6)
        self.assertAlmostEqual(summary["avg_trade_pnl_pct"], 0.025, places=6)

        equity = self.service._build_equity(trades=enriched, initial_capital=1000.0)
        self.assertAlmostEqual(equity["end_capital"], 1000.0, places=6)
        self.assertAlmostEqual(equity["net_return_pct"], 0.0, places=6)

    def test_build_equity_uses_additive_capital_pnl_amounts_for_levered_trade_series(self) -> None:
        trades = [
            {
                "position_id": "w1",
                "trade_date_ist": "2024-01-04",
                "exit_dt": None,
                "exit_time": "2024-01-04T11:21:00+05:30",
                "capital_pnl_amount": 867.0,
                "capital_pnl_pct": 0.867,
            },
            {
                "position_id": "w2",
                "trade_date_ist": "2024-01-05",
                "exit_dt": None,
                "exit_time": "2024-01-05T11:23:00+05:30",
                "capital_pnl_amount": 739.5,
                "capital_pnl_pct": 0.7395,
            },
            {
                "position_id": "l1",
                "trade_date_ist": "2024-01-08",
                "exit_dt": None,
                "exit_time": "2024-01-08T10:37:00+05:30",
                "capital_pnl_amount": -1413.75,
                "capital_pnl_pct": -1.41375,
            },
            {
                "position_id": "w3",
                "trade_date_ist": "2024-01-08",
                "exit_dt": None,
                "exit_time": "2024-01-08T11:17:00+05:30",
                "capital_pnl_amount": 687.75,
                "capital_pnl_pct": 0.68775,
            },
            {
                "position_id": "w4",
                "trade_date_ist": "2024-01-08",
                "exit_dt": None,
                "exit_time": "2024-01-08T12:08:00+05:30",
                "capital_pnl_amount": 284.25,
                "capital_pnl_pct": 0.28425,
            },
        ]

        equity = self.service._build_equity(trades=trades, initial_capital=1000.0)

        self.assertAlmostEqual(equity["end_capital"], 2164.75, places=6)
        self.assertAlmostEqual(equity["net_return_pct"], 1.16475, places=6)
        self.assertGreaterEqual(equity["max_drawdown_pct"], -1.0)
        self.assertEqual(len(equity["days"]), 3)

    def test_build_equity_daily_rows_use_additive_day_pnl(self) -> None:
        trades = [
            {
                "position_id": "d1",
                "trade_date_ist": "2024-01-01",
                "exit_dt": None,
                "exit_time": "2024-01-01T10:00:00+05:30",
                "capital_pnl_amount": 200.0,
                "capital_pnl_pct": 0.2,
            },
            {
                "position_id": "d2",
                "trade_date_ist": "2024-01-01",
                "exit_dt": None,
                "exit_time": "2024-01-01T11:00:00+05:30",
                "capital_pnl_amount": -50.0,
                "capital_pnl_pct": -0.05,
            },
            {
                "position_id": "d3",
                "trade_date_ist": "2024-01-02",
                "exit_dt": None,
                "exit_time": "2024-01-02T10:00:00+05:30",
                "capital_pnl_amount": 100.0,
                "capital_pnl_pct": 0.1,
            },
        ]

        equity = self.service._build_equity(trades=trades, initial_capital=1000.0)
        day_rows = {row["date"]: row for row in equity["days"]}

        self.assertAlmostEqual(day_rows["2024-01-01"]["equity_start"], 1000.0, places=6)
        self.assertAlmostEqual(day_rows["2024-01-01"]["equity_end"], 1150.0, places=6)
        self.assertAlmostEqual(day_rows["2024-01-01"]["day_pnl_amount"], 150.0, places=6)
        self.assertAlmostEqual(day_rows["2024-01-01"]["day_return_pct"], 0.15, places=6)
        self.assertAlmostEqual(day_rows["2024-01-02"]["equity_start"], 1150.0, places=6)
        self.assertAlmostEqual(day_rows["2024-01-02"]["equity_end"], 1250.0, places=6)
        self.assertAlmostEqual(day_rows["2024-01-02"]["day_return_pct"], 100.0 / 1150.0, places=6)

    def test_stop_analysis_reports_trailing_and_stop_metrics(self) -> None:
        trades = [
            {
                "exit_reason": "TRAILING_STOP",
                "entry_premium": 100.0,
                "exit_stop_price": 115.0,
                "high_water_premium": 125.0,
                "stop_loss_pct": 0.10,
                "target_pct": 0.30,
                "trailing_active": False,
                "orb_trail_active": True,
                "oi_trail_active": False,
            },
            {
                "exit_reason": "STOP_LOSS",
                "entry_premium": 100.0,
                "exit_stop_price": 90.0,
                "high_water_premium": 102.0,
                "stop_loss_pct": 0.20,
                "target_pct": 0.40,
                "trailing_active": False,
                "orb_trail_active": False,
                "oi_trail_active": False,
            },
            {
                "exit_reason": "TARGET_HIT",
                "entry_premium": 100.0,
                "exit_stop_price": None,
                "high_water_premium": 135.0,
                "stop_loss_pct": 0.15,
                "target_pct": 0.50,
                "trailing_active": False,
                "orb_trail_active": False,
                "oi_trail_active": True,
            },
        ]

        summary = self.service._stop_analysis(trades)

        self.assertEqual(summary["stop_loss_exits"], 1)
        self.assertAlmostEqual(summary["stop_loss_exit_pct"], 1.0 / 3.0, places=6)
        self.assertEqual(summary["trailing_stop_exits"], 1)
        self.assertAlmostEqual(summary["trailing_stop_exit_pct"], 1.0 / 3.0, places=6)
        self.assertEqual(summary["trailing_active_trades"], 2)
        self.assertAlmostEqual(summary["trailing_active_trade_pct"], 2.0 / 3.0, places=6)
        self.assertEqual(summary["generic_trailing_active_trades"], 0)
        self.assertEqual(summary["orb_trailing_active_trades"], 1)
        self.assertEqual(summary["oi_trailing_active_trades"], 1)
        self.assertAlmostEqual(summary["avg_locked_gain_pct_before_trailing_exit"], 0.15, places=6)
        self.assertAlmostEqual(summary["avg_trailing_profit_capture_pct"], 0.60, places=6)
        self.assertAlmostEqual(summary["avg_configured_stop_loss_pct"], 0.15, places=6)
        self.assertAlmostEqual(summary["avg_configured_target_pct"], 0.40, places=6)

    def test_trade_from_docs_marks_strategy_specific_trail_mechanism(self) -> None:
        trade = self.service._trade_from_docs(
            position_id="p1",
            docs={
                "open": {
                    "signal_id": "sig-1",
                    "timestamp": "2024-01-05T10:46:00+05:30",
                    "direction": "PE",
                    "strike": 48400,
                    "entry_premium": 415.55,
                    "lots": 1,
                    "lot_size": 15,
                    "stop_loss_pct": 0.4,
                    "target_pct": 0.8,
                    "trailing_enabled": False,
                    "reason": "[TRENDING] ORB: ORB_DOWN",
                },
                "close": {
                    "timestamp": "2024-01-05T11:23:00+05:30",
                    "exit_premium": 464.85,
                    "pnl_pct": 0.11863794970520998,
                    "bars_held": 37,
                    "mfe_pct": 0.22,
                    "mae_pct": -0.01,
                    "trailing_active": False,
                    "orb_trail_active": True,
                    "oi_trail_active": False,
                    "exit_reason": "TRAILING_STOP",
                    "stop_price": 467.36,
                    "high_water_premium": 508.0,
                },
                "open_doc": {"trade_date_ist": "2024-01-05"},
                "close_doc": {"trade_date_ist": "2024-01-05"},
            },
            signal_map={
                "sig-1": {
                    "signal_id": "sig-1",
                    "regime": "TRENDING",
                    "confidence": 0.38,
                    "reason": "[TRENDING] ORB: ORB_DOWN",
                    "decision_metrics": {
                        "entry_prob": 0.81,
                        "direction_trade_prob": 0.74,
                        "ce_prob": 0.22,
                        "pe_prob": 0.78,
                        "recipe_prob": 0.66,
                    },
                    "decision_reason_code": "pe_above_threshold",
                    "contributing_strategies": ["ORB"],
                }
            },
            cost_bps=0.0,
        )

        self.assertIsNotNone(trade)
        self.assertEqual(trade["exit_reason"], "TRAILING_STOP")
        self.assertEqual(trade["exit_mechanism"], "ORB_TRAIL")
        self.assertEqual(trade["signal_decision_reason_code"], "pe_above_threshold")
        self.assertAlmostEqual(trade["signal_decision_metrics"]["entry_prob"], 0.81, places=6)
        self.assertAlmostEqual(trade["signal_decision_metrics"]["recipe_prob"], 0.66, places=6)

    def test_exit_reason_breakdown_groups_and_sorts(self) -> None:
        trades = [
            {"exit_reason": "TRAILING_STOP", "pnl_pct_net": 0.15, "capital_pnl_pct": 0.01},
            {"exit_reason": "TRAILING_STOP", "pnl_pct_net": 0.05, "capital_pnl_pct": 0.03},
            {"exit_reason": "STOP_LOSS", "pnl_pct_net": -0.10, "capital_pnl_pct": -0.02},
            {"exit_reason": None, "pnl_pct_net": 0.0, "capital_pnl_pct": 0.0},
        ]

        rows = self.service._exit_reason_breakdown(trades)
        row_map = {str(row["exit_reason"]): row for row in rows}

        self.assertEqual([row["exit_reason"] for row in rows], ["TRAILING_STOP", "STOP_LOSS", "UNKNOWN"])
        self.assertEqual(row_map["TRAILING_STOP"]["count"], 2)
        self.assertAlmostEqual(row_map["TRAILING_STOP"]["pct"], 0.5, places=6)
        self.assertAlmostEqual(row_map["TRAILING_STOP"]["avg_pnl_pct_net"], 0.10, places=6)
        self.assertAlmostEqual(row_map["TRAILING_STOP"]["avg_capital_pnl_pct"], 0.02, places=6)
        self.assertEqual(row_map["STOP_LOSS"]["count"], 1)
        self.assertAlmostEqual(row_map["UNKNOWN"]["pct"], 0.25, places=6)

    def test_group_breakdown_uses_capital_metrics(self) -> None:
        trades = [
            {"entry_strategy": "OI_BUILDUP", "capital_pnl_pct": 0.02, "pnl_pct_net": 0.10},
            {"entry_strategy": "OI_BUILDUP", "capital_pnl_pct": -0.01, "pnl_pct_net": -0.05},
            {"entry_strategy": "ORB", "capital_pnl_pct": 0.005, "pnl_pct_net": 0.02},
        ]

        rows = self.service._group_breakdown(trades, "entry_strategy")
        row_map = {str(row["entry_strategy"]): row for row in rows}

        self.assertEqual(rows[0]["entry_strategy"], "OI_BUILDUP")
        self.assertEqual(row_map["OI_BUILDUP"]["trades"], 2)
        self.assertAlmostEqual(row_map["OI_BUILDUP"]["avg_capital_pnl_pct"], 0.005, places=6)
        self.assertAlmostEqual(row_map["OI_BUILDUP"]["total_capital_pnl_pct"], 0.01, places=6)
        self.assertAlmostEqual(row_map["OI_BUILDUP"]["avg_trade_pnl_pct"], 0.025, places=6)

    def test_iso_or_none_renders_naive_datetime_as_ist(self) -> None:
        rendered = _iso_or_none(datetime(2026, 3, 2, 7, 15, 0))

        self.assertEqual(rendered, "2026-03-02T12:45:00+05:30")


if __name__ == "__main__":
    unittest.main()
