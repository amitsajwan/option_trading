import unittest

from market_data_dashboard.real_source import (
    _find_underlying_stop_trigger,
    _position_to_trade,
)
from market_data_dashboard.schemas.monitor import MonitorCandle, MonitorSignal, MonitorSignalMetrics


class RealSourceTradeTests(unittest.TestCase):
    def test_find_underlying_stop_trigger_distinguishes_intrabar_and_close_breach(self) -> None:
        candles = [
            MonitorCandle(i=0, o=54006.15, h=54039.0, l=53995.10, c=54020.0, v=1, t=1, label="09:45"),
            MonitorCandle(i=1, o=53983.20, h=53984.0, l=53958.15, c=53975.45, v=1, t=2, label="10:06"),
            MonitorCandle(i=2, o=53968.65, h=53974.60, l=53952.0, c=53965.0, v=1, t=3, label="10:07"),
        ]

        trigger_candle, trigger_detail = _find_underlying_stop_trigger(
            direction="CE",
            stop_level=53965.98,
            entry_idx=0,
            exit_idx=2,
            candles=candles,
        )

        self.assertEqual(trigger_candle, "10:07")
        self.assertIn("at 10:07", trigger_detail)
        self.assertIn("first intrabar breach at 10:06", trigger_detail)

    def test_position_to_trade_enriches_underlying_stop_fields(self) -> None:
        candles = [
            MonitorCandle(i=0, o=54006.15, h=54039.0, l=53995.10, c=54020.0, v=1, t=1725423300000, label="09:45"),
            MonitorCandle(i=1, o=53983.20, h=53984.0, l=53958.15, c=53975.45, v=1, t=1725425160000, label="10:06"),
            MonitorCandle(i=2, o=53968.65, h=53974.60, l=53952.0, c=53965.0, v=1, t=1725425220000, label="10:07"),
        ]
        signal = MonitorSignal(
            t=1725423300000,
            idx=0,
            strat="ML_PURE_STAGED",
            dir="LONG",
            conf=0.61,
            fired=True,
            reason="staged_entry_ready",
            detail="ml_pure_staged: action=BUY_CE",
            metrics=MonitorSignalMetrics(
                entry_prob=0.55,
                trade_prob=0.55,
                up_prob=0.62,
                ce_prob=0.62,
                pe_prob=0.38,
                recipe_prob=0.63,
                recipe_margin=0.07,
            ),
            regime="UNKNOWN",
        )

        trade = _position_to_trade(
            "b148d344",
            {
                "timestamp": "2024-09-25T09:45:00+05:30",
                "direction": "CE",
                "lots": 5,
                "entry_premium": 120.95,
                "underlying_stop_pct": 0.001,
                "underlying_target_pct": 0.0025,
                "entry_futures_price": 54020.0,
                "max_hold_bars": 25,
            },
            {
                "timestamp": "2024-09-25T10:07:00+05:30",
                "exit_premium": 74.85,
                "pnl_pct": -0.3811492352,
                "exit_reason": "STOP_LOSS",
                "reason": "STOP_LOSS pnl=-38.11%",
            },
            {"timestamp": "2024-09-25T09:45:00+05:30"},
            {"timestamp": "2024-09-25T10:07:00+05:30"},
            signal,
            [c.t for c in candles],
            candles,
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.stopBasis, "underlying")
        self.assertAlmostEqual(trade.entryFuturesPrice or 0.0, 54020.0, places=6)
        self.assertAlmostEqual(trade.underlyingStopPrice or 0.0, 53965.98, places=6)
        self.assertEqual(trade.stopTriggerCandle, "10:07")
        self.assertIn("underlying stop on close", trade.stopTriggerDetail)


if __name__ == "__main__":
    unittest.main()
