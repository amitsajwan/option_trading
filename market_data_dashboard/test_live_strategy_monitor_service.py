import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from market_data_dashboard.live_strategy_monitor_service import LiveStrategyMonitorService
from market_data_dashboard.strategy_evaluation_service import StrategyEvaluationService


class LiveStrategyMonitorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LiveStrategyMonitorService(StrategyEvaluationService())

    def test_build_current_open_positions_uses_latest_manage_state(self) -> None:
        position_map = {
            "open-1": {
                "position_id": "open-1",
                "open": {
                    "signal_id": "sig-1",
                    "entry_strategy": "OI_BUILDUP",
                    "entry_regime": "PRE_EXPIRY",
                    "direction": "CE",
                    "strike": 60100,
                    "entry_premium": 100.0,
                    "lots": 2,
                    "stop_loss_pct": 0.40,
                    "stop_price": 60.0,
                    "target_pct": 0.80,
                    "trailing_enabled": True,
                    "reason": "[PRE_EXPIRY] OI_BUILDUP: entry",
                },
                "open_doc": {"timestamp": "2026-03-02T10:00:00Z"},
                "latest_manage": {
                    "current_premium": 108.0,
                    "pnl_pct": 0.08,
                    "bars_held": 3,
                    "stop_price": 66.0,
                    "high_water_premium": 110.0,
                    "trailing_active": True,
                },
                "latest_manage_doc": {"timestamp": "2026-03-02T10:03:00Z"},
            },
            "closed-1": {
                "position_id": "closed-1",
                "open": {"signal_id": "sig-2"},
                "close": {"exit_reason": "TARGET_HIT"},
            },
        }
        signal_map = {
            "sig-1": {
                "regime": "PRE_EXPIRY",
                "reason": "[PRE_EXPIRY] OI_BUILDUP: entry",
            }
        }

        rows = self.service.build_current_open_positions(position_map, signal_map, initial_capital=500000.0)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_id"], "open-1")
        self.assertEqual(rows[0]["strategy"], "OI_BUILDUP")
        self.assertEqual(rows[0]["regime"], "PRE_EXPIRY")
        self.assertAlmostEqual(rows[0]["current_premium"], 108.0, places=6)
        self.assertAlmostEqual(rows[0]["capital_at_risk"], 3000.0, places=6)
        self.assertAlmostEqual(rows[0]["unrealized_pnl_amount"], 240.0, places=6)
        self.assertAlmostEqual(rows[0]["unrealized_pnl_pct"], 240.0 / 500000.0, places=9)
        self.assertTrue(rows[0]["trailing_active"])

    def test_build_current_open_positions_uses_top_level_signal_id_fallback(self) -> None:
        position_map = {
            "open-legacy": {
                "position_id": "open-legacy",
                "open": {
                    "entry_strategy": "OI_BUILDUP",
                    "direction": "CE",
                    "entry_premium": 100.0,
                    "lots": 1,
                    "reason": "[PRE_EXPIRY] OI_BUILDUP: entry",
                },
                "open_doc": {
                    "signal_id": "sig-legacy-1",
                    "timestamp": "2026-03-02T10:00:00Z",
                },
                "latest_manage": {
                    "current_premium": 108.0,
                    "pnl_pct": 0.08,
                },
                "latest_manage_doc": {"timestamp": "2026-03-02T10:03:00Z"},
            },
        }
        signal_map = {
            "sig-legacy-1": {
                "regime": "PRE_EXPIRY",
                "reason": "[PRE_EXPIRY] OI_BUILDUP: entry",
            }
        }

        rows = self.service.build_current_open_positions(position_map, signal_map, initial_capital=500000.0)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signal_id"], "sig-legacy-1")
        self.assertEqual(rows[0]["strategy"], "OI_BUILDUP")
        self.assertEqual(rows[0]["regime"], "PRE_EXPIRY")

    def test_build_latest_closed_trade_reconstructs_capital_metrics(self) -> None:
        position_map = {
            "closed-1": {
                "position_id": "closed-1",
                "open": {
                    "signal_id": "sig-1",
                    "timestamp": "2026-03-02T10:00:00Z",
                    "direction": "PE",
                    "strike": 60000,
                    "entry_premium": 200.0,
                    "lots": 1,
                    "stop_loss_pct": 0.4,
                    "stop_price": 120.0,
                    "target_pct": 0.8,
                    "trailing_enabled": False,
                    "trailing_activation_pct": 0.1,
                    "trailing_offset_pct": 0.05,
                    "trailing_lock_breakeven": True,
                    "reason": "[TRENDING] EMA_CROSSOVER: bear",
                },
                "open_doc": {"trade_date_ist": "2026-03-02"},
                "close": {
                    "timestamp": "2026-03-02T10:02:00Z",
                    "direction": "PE",
                    "strike": 60000,
                    "entry_premium": 200.0,
                    "exit_premium": 220.0,
                    "pnl_pct": 0.10,
                    "mfe_pct": 0.12,
                    "mae_pct": -0.01,
                    "bars_held": 2,
                    "exit_reason": "TARGET_HIT",
                    "stop_price": 120.0,
                    "high_water_premium": 220.0,
                    "trailing_active": False,
                },
                "close_doc": {"trade_date_ist": "2026-03-02"},
            }
        }
        signal_map = {"sig-1": {"regime": "TRENDING", "reason": "[TRENDING] EMA_CROSSOVER: bear"}}

        trade = self.service.build_latest_closed_trade(position_map, signal_map, initial_capital=500000.0)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["position_id"], "closed-1")
        self.assertEqual(trade["entry_strategy"], "EMA_CROSSOVER")
        self.assertEqual(trade["exit_reason"], "TARGET_HIT")
        self.assertAlmostEqual(trade["capital_at_risk"], 3000.0, places=6)
        self.assertAlmostEqual(trade["capital_pnl_amount"], 300.0, places=6)
        self.assertAlmostEqual(trade["capital_pnl_pct"], 300.0 / 500000.0, places=9)

    def test_build_latest_closed_trade_uses_top_level_signal_id_fallback(self) -> None:
        position_map = {
            "closed-legacy": {
                "position_id": "closed-legacy",
                "open": {
                    "timestamp": "2026-03-02T10:00:00Z",
                    "direction": "PE",
                    "strike": 60000,
                    "entry_premium": 200.0,
                    "lots": 1,
                    "reason": "[TRENDING] EMA_CROSSOVER: bear",
                },
                "open_doc": {
                    "trade_date_ist": "2026-03-02",
                    "signal_id": "sig-legacy-close-1",
                },
                "close": {
                    "timestamp": "2026-03-02T10:02:00Z",
                    "direction": "PE",
                    "strike": 60000,
                    "entry_premium": 200.0,
                    "exit_premium": 220.0,
                    "pnl_pct": 0.10,
                    "mfe_pct": 0.12,
                    "mae_pct": -0.01,
                    "bars_held": 2,
                    "exit_reason": "TARGET_HIT",
                },
                "close_doc": {
                    "trade_date_ist": "2026-03-02",
                },
            }
        }
        signal_map = {
            "sig-legacy-close-1": {
                "regime": "TRENDING",
                "reason": "[TRENDING] EMA_CROSSOVER: bear",
            }
        }

        trade = self.service.build_latest_closed_trade(position_map, signal_map, initial_capital=500000.0)

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade["signal_id"], "sig-legacy-close-1")
        self.assertEqual(trade["entry_strategy"], "EMA_CROSSOVER")

    def test_build_chart_markers_emits_entry_and_exit_pairs(self) -> None:
        closed_trades = [
            {
                "position_id": "closed-1",
                "entry_strategy": "EMA_CROSSOVER",
                "regime": "TRENDING",
                "direction": "PE",
                "strike": 60000,
                "entry_time": "2026-03-02T10:00:00Z",
                "exit_time": "2026-03-02T10:02:00Z",
                "entry_premium": 200.0,
                "exit_premium": 220.0,
                "pnl_pct_net": 0.10,
                "exit_reason": "TARGET_HIT",
                "result": "WIN",
            }
        ]
        current_positions = [
            {
                "position_id": "open-1",
                "strategy": "OI_BUILDUP",
                "regime": "PRE_EXPIRY",
                "direction": "CE",
                "strike": 60100,
                "entry_time": "2026-03-02T10:03:00Z",
                "entry_premium": 108.0,
            }
        ]

        markers = self.service.build_chart_markers(closed_trades, current_positions)

        self.assertEqual(len(markers), 3)
        self.assertEqual(markers[0]["type"], "entry")
        self.assertEqual(markers[1]["type"], "exit")
        self.assertIn("+10.00%", markers[1]["label"])
        self.assertEqual(markers[2]["position_id"], "open-1")

    def test_build_freshness_handles_missing_and_stale_values(self) -> None:
        freshness = self.service.build_freshness(None, None, None)

        self.assertFalse(freshness["votes_fresh"])
        self.assertFalse(freshness["signals_fresh"])
        self.assertFalse(freshness["positions_fresh"])
        self.assertIsNone(freshness["latest_vote_age_sec"])

    def test_partition_open_positions_flags_stale_orphan(self) -> None:
        now_utc = datetime(2026, 3, 6, 5, 8, 0, tzinfo=timezone.utc)  # 10:38 IST
        current_open_positions = [
            {
                "position_id": "fresh-1",
                "entry_time": "2026-03-06T10:02:00+05:30",
                "current_time": "2026-03-06T10:38:00+05:30",
                "strategy": "EMA_CROSSOVER",
                "direction": "PE",
                "strike": 58800,
            },
            {
                "position_id": "stale-1",
                "entry_time": "2026-03-06T10:03:00+05:30",
                "current_time": "2026-03-06T10:06:00+05:30",
                "strategy": "EMA_CROSSOVER",
                "direction": "PE",
                "strike": 58800,
            },
        ]

        active, stale = self.service.partition_open_positions(
            current_open_positions=current_open_positions,
            latest_position_ts="2026-03-06T10:38:00+05:30",
            market_session_open=True,
            reference_time_utc=now_utc,
            stale_after_seconds=300,
        )

        self.assertEqual([row["position_id"] for row in active], ["fresh-1"])
        self.assertEqual([row["position_id"] for row in stale], ["stale-1"])
        self.assertEqual(stale[0]["stale_reason"], "lag_vs_latest_position")
        self.assertGreater(stale[0]["lag_vs_latest_position_sec"], 300)

    def test_load_session_underlying_chart_uses_session_time_labels(self) -> None:
        class CursorStub:
            def __init__(self, docs):
                self._docs = docs

            def sort(self, *_args, **_kwargs):
                return self

            def __iter__(self):
                return iter(self._docs)

        class CollectionStub:
            def find(self, *_args, **_kwargs):
                return CursorStub(
                    [
                        {
                            "timestamp": "2026-03-02T07:15:00Z",
                            "payload": {
                                "snapshot": {
                                    "session_context": {
                                        "timestamp": "2026-03-02T12:45:00",
                                        "time": "12:45:00",
                                    },
                                    "futures_bar": {"fut_close": 60025.0},
                                }
                            },
                        }
                    ]
                )

        fake_db = {"phase1_market_snapshots": CollectionStub()}
        self.service._evaluation_service._db = lambda: fake_db  # type: ignore[method-assign]

        chart = self.service.load_session_underlying_chart(date_ist="2026-03-02", instrument="BANKNIFTY26MARFUT")

        self.assertIsNotNone(chart)
        self.assertEqual(chart["labels"], ["12:45:00"])
        self.assertEqual(chart["timestamps"], ["2026-03-02T07:15:00Z"])

    def test_build_deterministic_diagnostics_exposes_policy_effectiveness_ratios(self) -> None:
        class CursorStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                return CursorStub(self._docs[: int(value)])

            def __iter__(self):
                return iter(self._docs)

        class CollectionStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, *_args, **_kwargs):
                return CursorStub(self._docs)

            def find_one(self, *_args, **_kwargs):
                return None

            def count_documents(self, query):
                if query.get("signal_type") == "ENTRY" and query.get("direction") == {"$in": ["CE", "PE"]}:
                    return 9
                if query.get("payload.vote.raw_signals._policy_reason") == {"$exists": True}:
                    return 10
                if query.get("payload.vote.raw_signals._policy_allowed") is True:
                    return 6
                if query.get("payload.vote.raw_signals._policy_allowed") is False:
                    return 4
                if query.get("payload.vote.raw_signals._entry_warmup_blocked") is True:
                    return 1
                return 0

        docs = [
            {
                "timestamp": "2026-03-06T12:32:00+05:30",
                "snapshot_id": "20260306_1232",
                "strategy": "EMA_CROSSOVER",
                "signal_type": "ENTRY",
                "direction": "PE",
                "confidence": 0.77,
                "reason": "EMA_BEAR",
                "payload": {
                    "vote": {
                        "raw_signals": {
                            "_policy_reason": "allowed score=0.54",
                            "_policy_allowed": True,
                            "_policy_score": 0.54,
                        }
                    }
                },
            }
        ]
        diagnostics = self.service.build_deterministic_diagnostics(
            date_ist="2026-03-06",
            votes_coll=CollectionStub(docs),
        )
        self.assertEqual(diagnostics["counts"]["policy_evaluated_votes_day"], 10)
        self.assertEqual(diagnostics["counts"]["policy_allowed_votes_day"], 6)
        self.assertEqual(diagnostics["counts"]["policy_blocked_votes_day"], 4)
        self.assertAlmostEqual(diagnostics["ratios"]["policy_pass_rate_day"], 0.6, places=6)
        self.assertAlmostEqual(diagnostics["ratios"]["policy_block_rate_day"], 0.4, places=6)
        self.assertAlmostEqual(diagnostics["ratios"]["warmup_block_rate_day"], 1.0 / 9.0, places=6)

    def test_build_ml_pure_diagnostics_tracks_hold_reasons_and_skew(self) -> None:
        class CursorStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                return CursorStub(self._docs[: int(value)])

            def __iter__(self):
                return iter(self._docs)

        class CollectionStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, *_args, **_kwargs):
                return CursorStub(self._docs)

        docs = [
            {
                "timestamp": "2026-03-07T09:30:00+05:30",
                "signal_id": "s1",
                "signal_type": "ENTRY",
                "direction": "CE",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "ce_above_threshold",
                "decision_metrics": {"entry_prob": 0.70, "ce_prob": 0.70, "pe_prob": 0.40, "edge": 0.30, "confidence": 0.70},
            },
            {
                "timestamp": "2026-03-07T09:31:00+05:30",
                "signal_id": "s2",
                "signal_type": "ENTRY",
                "direction": "PE",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "pe_above_threshold",
                "decision_metrics": {"entry_prob": 0.62, "ce_prob": 0.35, "pe_prob": 0.68, "edge": 0.33, "confidence": 0.68},
            },
            {
                "timestamp": "2026-03-07T09:32:00+05:30",
                "signal_id": "s3",
                "signal_type": "HOLD",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "low_edge_conflict",
                "decision_metrics": {"ce_prob": 0.61, "pe_prob": 0.60, "edge": 0.01, "confidence": 0.61},
            },
            {
                "timestamp": "2026-03-07T09:33:00+05:30",
                "signal_id": "s4",
                "signal_type": "HOLD",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "feature_stale",
                "decision_metrics": {"confidence": 0.60},
            },
        ]

        position_docs = [
            {
                "position_id": "p1",
                "signal_id": "s1",
                "event": "POSITION_OPEN",
                "timestamp": "2026-03-07T09:30:00+05:30",
                "trade_date_ist": "2026-03-07",
                "engine_mode": "ml_pure",
                "payload": {
                    "position": {
                        "signal_id": "s1",
                        "timestamp": "2026-03-07T09:30:00+05:30",
                        "direction": "CE",
                        "entry_premium": 100.0,
                        "lots": 1,
                        "stop_loss_pct": 0.05,
                        "target_pct": 0.20,
                        "reason": "[TRENDING] ML_PURE_STAGED: entry",
                    }
                },
            },
            {
                "position_id": "p1",
                "event": "POSITION_CLOSE",
                "timestamp": "2026-03-07T09:40:00+05:30",
                "trade_date_ist": "2026-03-07",
                "engine_mode": "ml_pure",
                "actual_outcome": "win",
                "actual_return_pct": 0.10,
                "payload": {
                    "position": {
                        "timestamp": "2026-03-07T09:40:00+05:30",
                        "exit_premium": 110.0,
                        "pnl_pct": 0.10,
                        "mfe_pct": 0.11,
                        "mae_pct": -0.02,
                        "bars_held": 10,
                        "exit_reason": "TARGET_HIT",
                    }
                },
            },
            {
                "position_id": "p2",
                "signal_id": "s2",
                "event": "POSITION_OPEN",
                "timestamp": "2026-03-07T09:31:00+05:30",
                "trade_date_ist": "2026-03-07",
                "engine_mode": "ml_pure",
                "payload": {
                    "position": {
                        "signal_id": "s2",
                        "timestamp": "2026-03-07T09:31:00+05:30",
                        "direction": "PE",
                        "entry_premium": 100.0,
                        "lots": 1,
                        "stop_loss_pct": 0.05,
                        "target_pct": 0.20,
                        "reason": "[TRENDING] ML_PURE_STAGED: entry",
                    }
                },
            },
            {
                "position_id": "p2",
                "event": "POSITION_CLOSE",
                "timestamp": "2026-03-07T09:41:00+05:30",
                "trade_date_ist": "2026-03-07",
                "engine_mode": "ml_pure",
                "actual_outcome": "loss",
                "actual_return_pct": -0.05,
                "payload": {
                    "position": {
                        "timestamp": "2026-03-07T09:41:00+05:30",
                        "exit_premium": 95.0,
                        "pnl_pct": -0.05,
                        "mfe_pct": 0.03,
                        "mae_pct": -0.06,
                        "bars_held": 10,
                        "exit_reason": "STOP_LOSS",
                    }
                },
            },
        ]

        with patch.dict("os.environ", {"ML_PURE_STAGE1_THRESHOLD": "0.60"}, clear=False):
            diagnostics = self.service.build_ml_pure_diagnostics(
                date_ist="2026-03-07",
                signals_coll=CollectionStub(docs),
                positions_coll=CollectionStub(position_docs),
            )

        self.assertEqual(diagnostics["counts"]["entries_ce"], 1)
        self.assertEqual(diagnostics["counts"]["entries_pe"], 1)
        self.assertEqual(diagnostics["counts"]["holds"], 2)
        self.assertEqual(diagnostics["hold_reasons"]["low_edge_conflict"], 1)
        self.assertEqual(diagnostics["hold_reasons"]["feature_stale"], 1)
        self.assertAlmostEqual(diagnostics["ratios"]["hold_rate"], 0.5, places=6)
        self.assertAlmostEqual(diagnostics["ratios"]["ce_vs_pe_skew"], 0.5, places=6)
        self.assertIn("rolling_quality", diagnostics)
        self.assertTrue(diagnostics["rolling_quality"]["stage1_precision"]["available"])
        self.assertAlmostEqual(diagnostics["rolling_quality"]["stage1_precision"]["precision"], 0.5, places=6)
        self.assertEqual(diagnostics["rolling_quality"]["status"], "ok")

    def test_build_ml_pure_diagnostics_surfaces_monitoring_failure(self) -> None:
        class CursorStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                return CursorStub(self._docs[: int(value)])

            def __iter__(self):
                return iter(self._docs)

        class CollectionStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, *_args, **_kwargs):
                return CursorStub(self._docs)

        docs = [
            {
                "timestamp": "2026-03-07T09:30:00+05:30",
                "signal_id": "s1",
                "signal_type": "ENTRY",
                "direction": "CE",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "ce_above_threshold",
                "decision_metrics": {"entry_prob": 0.70, "ce_prob": 0.70, "pe_prob": 0.30, "edge": 0.40, "confidence": 0.70},
            }
        ]

        with patch("market_data_dashboard.diagnostics.ml_pure._rolling_ml_quality_from_collections", side_effect=RuntimeError("mongo unavailable")):
            diagnostics = self.service.build_ml_pure_diagnostics(
                date_ist="2026-03-07",
                signals_coll=CollectionStub(docs),
                positions_coll=CollectionStub([]),
            )

        self.assertIn("rolling_quality", diagnostics)
        self.assertEqual(diagnostics["rolling_quality"]["status"], "error")
        self.assertEqual(diagnostics["rolling_quality"]["error"]["type"], "RuntimeError")
        self.assertEqual(diagnostics["rolling_quality"]["error"]["message"], "mongo unavailable")
        self.assertIn("30d monitoring error", diagnostics["summary"])

    def test_ml_pure_diagnostics_surfaces_rolling_quality_errors(self) -> None:
        class CursorStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, value):
                return CursorStub(self._docs[: int(value)])

            def __iter__(self):
                return iter(self._docs)

        class CollectionStub:
            def __init__(self, docs):
                self._docs = list(docs)

            def find(self, *_args, **_kwargs):
                return CursorStub(self._docs)

        docs = [
            {
                "timestamp": "2026-03-07T09:30:00+05:30",
                "trade_date_ist": "2026-03-07",
                "signal_id": "s1",
                "signal_type": "ENTRY",
                "direction": "CE",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_reason_code": "ce_above_threshold",
                "decision_metrics": {"entry_prob": 0.70, "ce_prob": 0.70, "pe_prob": 0.30, "edge": 0.40, "confidence": 0.70},
            }
        ]

        with patch(
            "market_data_dashboard.diagnostics.ml_pure._rolling_ml_quality_from_collections",
            side_effect=RuntimeError("bad-threshold-report"),
        ):
            diagnostics = self.service.build_ml_pure_diagnostics(
                date_ist="2026-03-07",
                signals_coll=CollectionStub(docs),
                positions_coll=CollectionStub([]),
            )

        self.assertIn("rolling_quality", diagnostics)
        self.assertEqual(diagnostics["rolling_quality"]["error"]["type"], "RuntimeError")
        self.assertIn("30d monitoring error", diagnostics["summary"])

    def test_ops_state_and_ui_hints_reflect_critical_conditions(self) -> None:
        alerts = [{"id": "data_stale_with_exposure", "severity": "critical"}]
        latest_decision = {"reason_code": "risk_halt"}
        ops_state = self.service.build_ops_state(
            market_session_open=True,
            engine_context={"active_engine_mode": "ml_pure"},
            freshness={"votes_fresh": False, "signals_fresh": True, "positions_fresh": True},
            latest_decision=latest_decision,
            active_alerts=alerts,
        )
        ui_hints = self.service.build_ui_hints(
            engine_context={"active_engine_mode": "ml_pure"},
            active_alerts=alerts,
            freshness={"votes_fresh": False, "signals_fresh": True, "positions_fresh": True},
            debug_view=False,
        )
        self.assertEqual(ops_state["market_state"], "open")
        self.assertEqual(ops_state["engine_state"], "ml_pure_active")
        self.assertEqual(ops_state["risk_state"], "halted")
        self.assertEqual(ops_state["data_health_state"], "critical")
        self.assertEqual(ui_hints["active_engine_panel"], "ml_pure")
        self.assertEqual(ui_hints["recommended_focus_panel"], "active_alerts")
        self.assertTrue(ui_hints["degraded_mode"])

    def test_infer_engine_context_prefers_latest_signal_engine(self) -> None:
        ctx = self.service.infer_engine_context(
            recent_votes=[
                {"engine_mode": "deterministic", "strategy_profile_id": "det_core_v1"},
            ],
            recent_signals=[
                {
                    "engine_mode": "ml_pure",
                    "strategy_family_version": "ML_PURE_STAGED_V1",
                    "strategy_profile_id": "ml_pure_staged_v1",
                }
            ],
        )

        self.assertEqual(ctx["active_engine_mode"], "ml_pure")
        self.assertEqual(ctx["strategy_family_version"], "ML_PURE_STAGED_V1")
        self.assertEqual(ctx["strategy_profile_id"], "ml_pure_staged_v1")
        self.assertEqual(self.service.promotion_lane_from_engine(ctx["active_engine_mode"]), "ml_pure")


if __name__ == "__main__":
    unittest.main()
