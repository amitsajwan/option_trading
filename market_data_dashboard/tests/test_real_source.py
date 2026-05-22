import unittest

from market_data_dashboard.real_source import (
    _enforce_replay_integrity,
    _find_underlying_stop_trigger,
    _latest_run_id_for_date,
    _position_to_trade,
)
from market_data_dashboard.state.replay_integrity import replay_integrity_warnings
from market_data_dashboard.schemas.monitor import MonitorCandle, MonitorSignal, MonitorSignalMetrics


class RealSourceTradeTests(unittest.TestCase):
    def test_replay_integrity_warns_on_overlapping_trades(self) -> None:
        warnings = replay_integrity_warnings(
            [
                {"position_id": "p1", "entryIdx": 10, "exitIdx": 50},
                {"position_id": "p2", "entryIdx": 20, "exitIdx": 30},
                {"position_id": "p3", "entryIdx": 60, "exitIdx": 70},
            ]
        )

        self.assertIn("overlapping_replay_positions_detected", warnings)

    def test_replay_integrity_accepts_sequential_trades(self) -> None:
        warnings = replay_integrity_warnings(
            [
                {"position_id": "p1", "entryIdx": 10, "exitIdx": 20},
                {"position_id": "p2", "entryIdx": 20, "exitIdx": 30},
            ]
        )

        self.assertEqual(warnings, [])

    def test_enforce_replay_integrity_suppresses_overlapping_trades(self) -> None:
        """If overlap is detected, the trade list MUST be blanked AND an alert
        appended. The dropdown count already excludes contaminated runs; the
        grid must do the same so dropdown and grid agree."""
        contaminated = [
            {"position_id": "p1", "entryIdx": 10, "exitIdx": 50},
            {"position_id": "p2", "entryIdx": 20, "exitIdx": 30},  # overlaps p1
            {"position_id": "p3", "entryIdx": 60, "exitIdx": 70},
        ]
        trades, alerts = _enforce_replay_integrity(contaminated, [])
        self.assertEqual(trades, [], "overlapping trades must be suppressed from grid")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "warn")
        self.assertIn("overlapping positions", alerts[0].msg)
        self.assertIn("suppressed", alerts[0].msg)

    def test_enforce_replay_integrity_passthrough_for_clean_trades(self) -> None:
        clean = [
            {"position_id": "p1", "entryIdx": 10, "exitIdx": 20},
            {"position_id": "p2", "entryIdx": 25, "exitIdx": 35},
        ]
        trades, alerts = _enforce_replay_integrity(clean, [])
        self.assertEqual(len(trades), 2, "clean trades must pass through unchanged")
        self.assertEqual(alerts, [], "no alerts for clean trade list")

    def test_enforce_replay_integrity_preserves_existing_alerts(self) -> None:
        """Pre-existing alerts must not be lost when overlap is detected."""
        from market_data_dashboard.schemas.monitor import MonitorAlert
        existing = MonitorAlert(level="info", t="09:30", msg="warmup complete", tms=1)
        contaminated = [
            {"position_id": "p1", "entryIdx": 10, "exitIdx": 50},
            {"position_id": "p2", "entryIdx": 20, "exitIdx": 30},
        ]
        trades, alerts = _enforce_replay_integrity(contaminated, [existing])
        self.assertEqual(trades, [])
        self.assertEqual(len(alerts), 2)
        self.assertIs(alerts[0], existing)
        self.assertEqual(alerts[1].level, "warn")

    def test_latest_run_id_prefers_position_run_over_empty_registered_run(self) -> None:
        class _Collection:
            def __init__(self, docs):
                self._docs = list(docs)

            def find_one(self, query, projection=None, sort=None):  # noqa: ARG002
                rows = []
                for doc in self._docs:
                    ok = True
                    for key, value in query.items():
                        if key == "run_id" and value == {"$nin": [None, ""]}:
                            ok = doc.get("run_id") not in (None, "")
                        elif isinstance(value, dict) and "$lte" in value:
                            ok = str(doc.get(key) or "") <= str(value["$lte"])
                        elif isinstance(value, dict) and "$gte" in value:
                            ok = str(doc.get(key) or "") >= str(value["$gte"])
                        else:
                            ok = doc.get(key) == value
                        if not ok:
                            break
                    if ok:
                        rows.append(doc)
                if sort:
                    for field, direction in reversed(sort):
                        rows.sort(key=lambda item: str(item.get(field) or ""), reverse=direction < 0)
                return rows[0] if rows else None

        db = {
            "strategy_eval_runs": _Collection(
                [
                    {
                        "_id": "2",
                        "status": "completed",
                        "date_from": "2024-09-01",
                        "date_to": "2024-09-30",
                        "run_id": "empty-registered-run",
                    }
                ]
            ),
            "strategy_positions_historical": _Collection(
                [
                    {
                        "_id": "1",
                        "trade_date_ist": "2024-09-18",
                        "event": "POSITION_CLOSE",
                        "timestamp": "2024-09-18T15:26:00+05:30",
                        "run_id": "positions-run",
                    }
                ]
            ),
        }

        self.assertEqual(_latest_run_id_for_date(db, "2024-09-18"), "positions-run")

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
