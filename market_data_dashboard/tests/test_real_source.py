import unittest

from market_data_dashboard.real_source import (
    _enforce_replay_integrity,
    _find_underlying_stop_trigger,
    _load_entry_context_by_snapshot,
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
                "entry_snapshot_id": "snap-entry-1",
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
        self.assertEqual(trade.entrySnapshotId, "snap-entry-1")
        self.assertEqual(trade.entryContext, {})

    def test_load_entry_context_by_snapshot_merges_trace_and_selected_vote(self) -> None:
        class _Cursor:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *_args, **_kwargs):
                return self

            def __iter__(self):
                return iter(self._docs)

        class _Collection:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, query, projection=None):  # noqa: ARG002
                rows = []
                for doc in self._docs:
                    if str(doc.get("trade_date_ist") or "") != str(query.get("trade_date_ist") or ""):
                        continue
                    run_id = str(query.get("run_id") or "").strip()
                    if run_id and str(doc.get("run_id") or "").strip() != run_id:
                        continue
                    snap_filter = query.get("snapshot_id") or {}
                    snap_values = set(snap_filter.get("$in") or []) if isinstance(snap_filter, dict) else set()
                    if snap_values and str(doc.get("snapshot_id") or "") not in snap_values:
                        continue
                    for key, value in query.items():
                        if key in {"trade_date_ist", "run_id", "snapshot_id"}:
                            continue
                        if doc.get(key) != value:
                            break
                    else:
                        rows.append(doc)
                return _Cursor(rows)

        db = {
            "strategy_votes_historical": _Collection(
                [
                    {
                        "trade_date_ist": "2024-09-25",
                        "run_id": "run-1",
                        "snapshot_id": "snap-entry-1",
                        "strategy": "ML_ENTRY",
                        "direction": "PE",
                        "confidence": 0.81,
                        "reason": "ml_entry+consensus: PE margin=2.90",
                        "decision_metrics": {"policy_score": 0.85},
                        "raw_signals": {
                            "direction_source": "direction_consensus",
                            "direction_consensus_ce": 1.2,
                            "direction_consensus_pe": 4.1,
                            "direction_consensus_margin": 2.9,
                            "_policy_reason": "allowed score=0.85",
                            "_policy_checks": {
                                "volume": "PASS:vol_ratio=1.40",
                                "momentum": "PASS:r5m=-0.0040,r15m=-0.0060",
                            },
                        },
                    },
                    {
                        "trade_date_ist": "2024-09-25",
                        "run_id": "run-1",
                        "snapshot_id": "snap-entry-1",
                        "strategy": "ORB_BREAK",
                        "direction": "CE",
                        "confidence": 0.55,
                        "reason": "orb breakout",
                        "decision_metrics": {},
                        "raw_signals": {},
                    },
                ]
            ),
            "strategy_decision_traces_historical": _Collection(
                [
                    {
                        "trade_date_ist": "2024-09-25",
                        "run_id": "run-1",
                        "snapshot_id": "snap-entry-1",
                        "evaluation_type": "entry",
                        "final_outcome": "entry_taken",
                        "summary_metrics": {
                            "entry_prob": 0.82,
                            "shadow_score": -3.0,
                            "shadow_dir": "PE",
                            "shadow_basis": "multi_signal_pe(score=-3.0:below_vwap,pe_prem_dominant,r5m_dn)",
                        },
                        "selected_strategy_name": "ML_ENTRY",
                        "selected_direction": "PE",
                        "payload": {
                            "trace": {
                                "regime_context": {"regime": "TRENDING"},
                                "flow_gates": [{"gate_id": "policy_checks", "status": "pass"}],
                                "candidates": [
                                    {"strategy_name": "ML_ENTRY", "direction": "PE", "selected": True},
                                ],
                            }
                        },
                    }
                ]
            ),
        }
        class _Db(dict):
            def list_collection_names(self):
                return ["strategy_votes_historical", "strategy_decision_traces_historical"]

        ctx = _load_entry_context_by_snapshot(
            _Db(db),
            trade_date="2024-09-25",
            coll_votes="strategy_votes_historical",
            coll_traces="strategy_decision_traces_historical",
            run_id="run-1",
            snapshot_meta={"snap-entry-1": {"strategy": "ML_ENTRY", "direction": "PE"}},
        )

        self.assertIn("snap-entry-1", ctx)
        row = ctx["snap-entry-1"]
        self.assertEqual(row["selectedVote"]["strategy"], "ML_ENTRY")
        self.assertEqual(row["selectedVote"]["direction"], "PE")
        self.assertAlmostEqual(row["selectedVote"]["raw_signals"]["direction_consensus_pe"], 4.1, places=6)
        self.assertEqual(row["traceSummary"]["shadow_dir"], "PE")
        self.assertEqual(row["selectedCandidate"]["strategy_name"], "ML_ENTRY")
        self.assertEqual(row["regimeContext"]["regime"], "TRENDING")


if __name__ == "__main__":
    unittest.main()
