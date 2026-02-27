import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import ml_pipeline.paper_capital_runner as pcr
from ml_pipeline.config import TrainConfig
from ml_pipeline.live_inference_adapter import DecisionThresholds
from ml_pipeline.train_baseline import build_baseline_pipeline


def _build_model_package() -> dict:
    n = 120
    x1 = np.linspace(-2.0, 2.0, n)
    x2 = np.sin(np.linspace(0, 8, n))
    frame = pd.DataFrame({"feature_a": x1, "feature_b": x2})
    y_ce = (x1 + 0.3 * x2 > 0).astype(int)
    y_pe = (x2 - 0.2 * x1 > 0).astype(int)

    cfg = TrainConfig(
        train_ratio=0.7,
        valid_ratio=0.15,
        random_state=5,
        max_depth=3,
        n_estimators=80,
        learning_rate=0.05,
    )
    ce = build_baseline_pipeline(cfg)
    pe = build_baseline_pipeline(cfg)
    ce.fit(frame, y_ce)
    pe.fit(frame, y_pe)
    return {
        "kind": "unit_test_package",
        "feature_columns": ["feature_a", "feature_b"],
        "train_config": {
            "random_state": cfg.random_state,
        },
        "models": {
            "ce": ce,
            "pe": pe,
        },
    }


class PaperCapitalRunnerTests(unittest.TestCase):
    def test_option_ltp_extractor_requires_explicit_strike_match_or_atm(self) -> None:
        chain = {
            "strikes": [
                {"strike": 51500, "ce_ltp": 99.0, "pe_ltp": 0.45},
                {"strike": 51700, "ce_ltp": 0.55, "pe_ltp": 102.0},
            ]
        }
        # No atm_strike provided => do not pick arbitrary first row for strike=None.
        self.assertIsNone(pcr._extract_option_ltp_for_side(chain, "PE", strike=None))
        # Explicit strike must match exactly; nearest-strike fallback is disabled.
        self.assertIsNone(pcr._extract_option_ltp_for_side(chain, "PE", strike=51600))
        self.assertAlmostEqual(float(pcr._extract_option_ltp_for_side(chain, "PE", strike=51500)), 0.45, places=6)

    def test_live_redis_capital_loop_updates_mtm_and_realized_capital(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        ts_2 = "2023-06-15T09:17:00+05:30"
        messages = [
            {"timestamp": ts_0},
            {"timestamp": ts_1},
            {"timestamp": ts_2},
        ]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 110.0, "opt_0_pe_close": 91.0},
            {"timestamp": ts_2, "trade_date": "2023-06-15", "opt_0_ce_close": 120.0, "opt_0_pe_close": 92.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.1
            elif idx == 1:
                action = "HOLD"
                ce_prob, pe_prob = 0.7, 0.2
            else:
                action = "BUY_PE"
                ce_prob, pe_prob = 0.2, 0.8
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=3,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    max_hold_minutes=5,
                    confidence_buffer=0.05,
                )
                self.assertEqual(summary["bars_processed"], 3)
                self.assertEqual(summary["trades_closed"], 1)
                self.assertAlmostEqual(summary["final"]["ce_capital_mtm"], 1200.0, places=6)
                self.assertAlmostEqual(summary["final"]["pe_capital_mtm"], 1000.0, places=6)
                self.assertAlmostEqual(summary["final"]["total_capital_mtm"], 2200.0, places=6)
                self.assertTrue(output.exists())
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 3)
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "MANAGE")
                self.assertEqual(events[2]["event_type"], "EXIT")
                self.assertAlmostEqual(float(events[2]["capital"]["total_capital_mtm"]), 2200.0, places=6)
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_stop_loss_exit_triggers_before_model_exit(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 89.0, "opt_0_pe_close": 91.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.1
            else:
                action = "HOLD"
                ce_prob, pe_prob = 0.49, 0.2
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_stop.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=2,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    stop_loss_pct=0.10,
                    model_exit_policy="signal_only",
                )
                self.assertEqual(summary["bars_processed"], 2)
                self.assertEqual(summary["trades_closed"], 1)
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 2)
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "EXIT")
                self.assertEqual(events[1]["event_reason"], "stop_loss")
                self.assertAlmostEqual(float(events[1]["capital"]["ce_capital_mtm"]), 890.0, places=6)
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_stop_only_policy_suppresses_model_signal_flip_exit(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 110.0, "opt_0_pe_close": 91.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.1
            else:
                action = "BUY_PE"  # strict policy would exit on signal_flip
                ce_prob, pe_prob = 0.2, 0.8
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_stop_only.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=2,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    stop_loss_pct=0.10,
                    model_exit_policy="stop_only",
                )
                self.assertEqual(summary["bars_processed"], 2)
                self.assertEqual(summary["trades_closed"], 1)
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 3)
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "MANAGE")
                self.assertEqual(events[1]["event_reason"], "hold_model_policy")
                self.assertEqual(events[2]["event_type"], "EXIT")
                self.assertEqual(events[2]["event_reason"], "session_end")
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_training_parity_policy_suppresses_signal_flip_and_confidence_fade(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.6, pe=0.6, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        ts_2 = "2023-06-15T09:17:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}, {"timestamp": ts_2}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 101.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_2, "trade_date": "2023-06-15", "opt_0_ce_close": 99.0, "opt_0_pe_close": 90.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.1
            elif idx == 1:
                action = "BUY_PE"  # strict policy would exit on signal_flip
                ce_prob, pe_prob = 0.2, 0.8
            else:
                action = "HOLD"  # strict policy would exit on confidence_fade for CE side
                ce_prob, pe_prob = 0.4, 0.3
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_training_parity.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=3,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    stop_loss_pct=0.10,
                    model_exit_policy="training_parity",
                )
                self.assertEqual(summary["bars_processed"], 3)
                self.assertEqual(summary["trades_closed"], 1)
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 4)
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "MANAGE")
                self.assertEqual(events[1]["event_reason"], "hold_model_policy")
                self.assertEqual(events[2]["event_type"], "MANAGE")
                self.assertEqual(events[2]["event_reason"], "hold_model_policy")
                self.assertEqual(events[3]["event_type"], "EXIT")
                self.assertEqual(events[3]["event_reason"], "session_end")
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_stagnation_exit_triggers_on_low_movement_window(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T11:15:00+05:30"
        ts_1 = "2023-06-15T11:16:00+05:30"
        ts_2 = "2023-06-15T11:17:00+05:30"
        ts_3 = "2023-06-15T11:18:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}, {"timestamp": ts_2}, {"timestamp": ts_3}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.00, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 100.20, "opt_0_pe_close": 90.0},
            {"timestamp": ts_2, "trade_date": "2023-06-15", "opt_0_ce_close": 100.10, "opt_0_pe_close": 90.0},
            {"timestamp": ts_3, "trade_date": "2023-06-15", "opt_0_ce_close": 100.15, "opt_0_pe_close": 90.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.2
            else:
                action = "HOLD"
                ce_prob, pe_prob = 0.7, 0.2
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_stagnation.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=4,
                    max_hold_minutes=20,
                    confidence_buffer=0.05,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    model_exit_policy="strict",
                    stagnation_enabled=True,
                    stagnation_window_minutes=3,
                    stagnation_threshold_pct=0.003,
                    stagnation_volatility_multiplier=0.0,
                    stagnation_min_hold_minutes=0,
                )
                self.assertEqual(summary["bars_processed"], 4)
                self.assertGreaterEqual(int(summary["event_reason_counts"].get("stagnation", 0)), 1)
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines]
                exit_reasons = [str(x.get("event_reason")) for x in events if str(x.get("event_type")) == "EXIT"]
                self.assertIn("stagnation", exit_reasons)
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_tick_level_stop_limit_pending_then_fill_between_bars(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:15:20+05:30"
        ts_2 = "2023-06-15T09:15:40+05:30"
        messages = [
            {"type": "message", "channel": "market:ohlc:BANKNIFTY-I:1m", "data": {"event_time": ts_0, "kind": "bar_open"}},
            {
                "type": "message",
                "channel": "market:options:BANKNIFTY-I",
                "data": {
                    "event_time": ts_1,
                    "payload": {
                        "atm_strike": 45000,
                        "strikes": [{"strike": 45000, "ce_ltp": 88.0, "pe_ltp": 100.0}],
                    },
                },
            },
            {
                "type": "message",
                "channel": "market:options:BANKNIFTY-I",
                "data": {
                    "event_time": ts_2,
                    "payload": {
                        "atm_strike": 45000,
                        "strikes": [{"strike": 45000, "ce_ltp": 89.2, "pe_ltp": 100.0}],
                    },
                },
            },
        ]
        rows = [
            {
                "timestamp": ts_0,
                "trade_date": "2023-06-15",
                "opt_0_ce_close": 100.0,
                "opt_0_pe_close": 90.0,
                "atm_strike": 45000,
                "expiry_code": "26MAR",
            }
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                payload = msg.get("data")
                kind = payload.get("kind") if isinstance(payload, dict) else None
                if kind == "bar_open":
                    if not self._rows:
                        return None
                    self._latest = self._rows.pop(0)
                    return str(self._latest["timestamp"])
                return None

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.9, 0.1
            else:
                action = "HOLD"
                ce_prob, pe_prob = 0.4, 0.2
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_stop_limit_tick.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=10,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    stop_loss_pct=0.10,
                    stop_execution_mode="stop_limit",
                    stop_limit_offset_pct=0.01,
                    stop_limit_max_wait_events=5,
                )
                self.assertEqual(summary["bars_processed"], 1)
                self.assertEqual(summary["trades_closed"], 1)
                lines = output.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines]
                # ENTRY on bar close, then stop pending tick, then stop-limit fill tick.
                self.assertEqual(len(events), 3)
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "MANAGE")
                self.assertIn("limit_pending", str(events[1]["event_reason"]))
                self.assertEqual(events[2]["event_type"], "EXIT")
                self.assertIn("limit_fill", str(events[2]["event_reason"]))
                self.assertAlmostEqual(float(events[2]["capital"]["ce_capital_mtm"]), 892.0, places=6)
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_runtime_guard_halt_suppresses_new_entry_after_loss_streak(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        ts_2 = "2023-06-15T09:17:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}, {"timestamp": ts_2}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 90.0, "opt_0_pe_close": 100.0},
            {"timestamp": ts_2, "trade_date": "2023-06-15", "opt_0_ce_close": 101.0, "opt_0_pe_close": 89.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.1
            elif idx == 1:
                action = "BUY_PE"  # signal flip exit at loss for CE
                ce_prob, pe_prob = 0.1, 0.8
            else:
                action = "BUY_CE"  # should be suppressed by runtime guard
                ce_prob, pe_prob = 0.9, 0.1
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_guard_halt.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=3,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    runtime_guard_max_consecutive_losses=1,
                )
                self.assertEqual(summary["bars_processed"], 3)
                self.assertEqual(summary["trades_closed"], 1)
                self.assertTrue(bool(summary["runtime_guard_state"]["is_halted"]))
                self.assertEqual(summary["runtime_guard_state"]["halt_reason"], "consecutive_losses")

                lines = output.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "EXIT")
                self.assertEqual(events[1]["event_reason"], "signal_flip")
                self.assertEqual(events[2]["event_type"], "IDLE")
                self.assertEqual(events[2]["event_reason"], "runtime_guard_halt")
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict

    def test_quality_daily_cap_blocks_second_entry_same_day(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        ts_0 = "2023-06-15T09:15:00+05:30"
        ts_1 = "2023-06-15T09:16:00+05:30"
        ts_2 = "2023-06-15T09:17:00+05:30"
        messages = [{"timestamp": ts_0}, {"timestamp": ts_1}, {"timestamp": ts_2}]
        rows = [
            {"timestamp": ts_0, "trade_date": "2023-06-15", "opt_0_ce_close": 100.0, "opt_0_pe_close": 90.0},
            {"timestamp": ts_1, "trade_date": "2023-06-15", "opt_0_ce_close": 95.0, "opt_0_pe_close": 98.0},
            {"timestamp": ts_2, "trade_date": "2023-06-15", "opt_0_ce_close": 110.0, "opt_0_pe_close": 90.0},
        ]

        class _StubPubSub:
            def __init__(self, payloads):
                self._rows = list(payloads)

            def psubscribe(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def get_message(self, timeout=0.0):
                if not self._rows:
                    return None
                return self._rows.pop(0)

            def close(self):
                return None

        class _StubRedis:
            def __init__(self, *args, **kwargs):
                self._pubsub = _StubPubSub(messages)

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

        class _StubFeatureClient:
            def __init__(self, instrument, max_bars=120, redis_client=None, mode_hint=None):
                self._rows = list(rows)
                self._latest = None

            def consume_redis_message(self, msg):
                if not self._rows:
                    return None
                self._latest = self._rows.pop(0)
                return str(self._latest["timestamp"])

            def build_latest_feature_row(self):
                return dict(self._latest)

        original_redis = pcr.redis.Redis
        original_feature_client = pcr.RedisEventFeatureClient
        original_predict = pcr.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode):
            idx = state["idx"]
            state["idx"] = idx + 1
            ts = str(row["timestamp"])
            if idx == 0:
                action = "BUY_CE"
                ce_prob, pe_prob = 0.8, 0.2
            elif idx == 1:
                action = "BUY_PE"  # exits CE via signal flip
                ce_prob, pe_prob = 0.2, 0.8
            else:
                action = "BUY_CE"  # should be blocked by daily cap
                ce_prob, pe_prob = 0.85, 0.15
            return {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "timestamp": ts,
                "trade_date": str(pd.Timestamp(ts).date()),
                "mode": mode,
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "ce_threshold": float(thresholds.ce),
                "pe_threshold": float(thresholds.pe),
                "action": action,
                "confidence": float(max(ce_prob, pe_prob)),
            }

        pcr.redis.Redis = _StubRedis
        pcr.RedisEventFeatureClient = _StubFeatureClient
        pcr.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "paper_capital_quality_daily_cap.jsonl"
                summary = pcr.run_live_redis_capital_loop(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    initial_ce_capital=1000.0,
                    initial_pe_capital=1000.0,
                    output_jsonl=output,
                    mode="dual",
                    max_iterations=3,
                    max_idle_seconds=0.01,
                    fee_bps=0.0,
                    quality_max_entries_per_day=1,
                )
                self.assertEqual(summary["bars_processed"], 3)
                self.assertEqual(summary["trades_closed"], 1)
                self.assertEqual(summary["quality_policy_state"]["entries_taken_total"], 1)

                lines = output.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines]
                self.assertEqual(events[0]["event_type"], "ENTRY")
                self.assertEqual(events[1]["event_type"], "EXIT")
                self.assertEqual(events[2]["event_type"], "IDLE")
                self.assertEqual(events[2]["event_reason"], "quality_block_daily_cap")
        finally:
            pcr.redis.Redis = original_redis
            pcr.RedisEventFeatureClient = original_feature_client
            pcr.predict_decision_from_row = original_predict


if __name__ == "__main__":
    unittest.main()
