import json
import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import ml_pipeline.live_inference_adapter as lia
from ml_pipeline.config import TrainConfig
from ml_pipeline.live_inference_adapter import (
    DecisionThresholds,
    _normalize_timestamp_string,
    load_thresholds,
    infer_action,
    predict_decision_from_row,
    run_live_api_paper_loop_v2,
    run_live_redis_event_loop_v2,
    run_replay_dry_run,
    run_replay_dry_run_v2,
)
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


class LiveInferenceAdapterTests(unittest.TestCase):
    def test_load_thresholds_supports_t08_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t08_threshold.json"
            p.write_text(
                json.dumps(
                    {
                        "ce": {"selected_threshold": 0.61},
                        "pe": {"selected_threshold": 0.59},
                        "decision_config": {"cost_per_trade": 0.0008},
                    }
                ),
                encoding="utf-8",
            )
            t = load_thresholds(p)
            self.assertAlmostEqual(t.ce, 0.61, places=12)
            self.assertAlmostEqual(t.pe, 0.59, places=12)
            self.assertAlmostEqual(t.cost_per_trade, 0.0008, places=12)

    def test_load_thresholds_supports_t31_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t31_threshold.json"
            p.write_text(
                json.dumps(
                    {
                        "dual_mode_policy": {"ce_threshold": 0.76, "pe_threshold": 0.67},
                        "decision_config": {"cost_per_trade": 0.0006},
                    }
                ),
                encoding="utf-8",
            )
            t = load_thresholds(p)
            self.assertAlmostEqual(t.ce, 0.76, places=12)
            self.assertAlmostEqual(t.pe, 0.67, places=12)
            self.assertAlmostEqual(t.cost_per_trade, 0.0006, places=12)

    def test_infer_action_modes(self) -> None:
        self.assertEqual(infer_action(0.8, 0.2, 0.5, 0.5, mode="dual"), "BUY_CE")
        self.assertEqual(infer_action(0.2, 0.8, 0.5, 0.5, mode="dual"), "BUY_PE")
        self.assertEqual(infer_action(0.6, 0.7, 0.5, 0.5, mode="dual"), "BUY_PE")
        self.assertEqual(infer_action(0.6, 0.7, 0.5, 0.5, mode="ce_only"), "BUY_CE")
        self.assertEqual(infer_action(0.4, 0.7, 0.5, 0.5, mode="ce_only"), "HOLD")
        self.assertEqual(infer_action(0.6, 0.7, 0.5, 0.5, mode="pe_only"), "BUY_PE")

    def test_normalize_timestamp_string_mixed_timezone_suffix(self) -> None:
        self.assertEqual(
            _normalize_timestamp_string("2026-02-21T17:04:00+05:30Z"),
            "2026-02-21T17:04:00+05:30",
        )
        self.assertEqual(
            _normalize_timestamp_string("2026-02-21 17:04:00+0530"),
            "2026-02-21T17:04:00+05:30",
        )

    def test_replay_dry_run_integration(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)
        rows = 40
        ts = pd.date_range("2023-06-15 09:15:00", periods=rows, freq="min")
        feat = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "feature_a": np.linspace(-1.0, 1.0, rows),
                "feature_b": np.cos(np.linspace(0, 4, rows)),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            feature_path = Path(tmp) / "features.parquet"
            out_path = Path(tmp) / "decisions.jsonl"
            feat.to_parquet(feature_path, index=False)
            summary = run_replay_dry_run(
                feature_parquet=feature_path,
                model_package=pkg,
                thresholds=thresholds,
                output_jsonl=out_path,
                mode="dual",
                limit=25,
            )
            self.assertEqual(summary["rows_processed"], 25)
            self.assertEqual(summary["decisions_emitted"], 25)
            self.assertTrue(out_path.exists())
            lines = out_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 25)
            sample = json.loads(lines[0])
            self.assertIn(sample["action"], {"BUY_CE", "BUY_PE", "HOLD"})
            self.assertIn("ce_prob", sample)
            self.assertIn("pe_prob", sample)

    def test_predict_decision_from_row(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)
        row = {"timestamp": "2023-06-15T09:15:00", "trade_date": "2023-06-15", "feature_a": 0.9, "feature_b": 0.1}
        decision = predict_decision_from_row(row, model_package=pkg, thresholds=thresholds, mode="dual")
        self.assertIn(decision["action"], {"BUY_CE", "BUY_PE", "HOLD"})
        self.assertGreaterEqual(decision["ce_prob"], 0.0)
        self.assertLessEqual(decision["ce_prob"], 1.0)
        self.assertGreaterEqual(decision["pe_prob"], 0.0)
        self.assertLessEqual(decision["pe_prob"], 1.0)

    def test_predict_decision_blocks_incomplete_row_when_required(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)
        row = {"timestamp": "2023-06-15T09:15:00", "trade_date": "2023-06-15", "feature_a": 0.9}
        decision = predict_decision_from_row(
            row,
            model_package=pkg,
            thresholds=thresholds,
            mode="dual",
            require_complete_row_inputs=True,
        )
        self.assertEqual(decision["action"], "HOLD")
        self.assertEqual(decision["decision_reason"], "model_input_incomplete")
        self.assertFalse(bool(decision["input_ready"]))
        self.assertEqual(int(decision["input_contract_missing_required_count"]), 1)

    def test_replay_dry_run_v2_state_persistence(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)
        rows = 30
        ts = pd.date_range("2023-06-15 09:15:00", periods=rows, freq="min")
        feat = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "feature_a": np.linspace(1.0, -1.0, rows),
                "feature_b": np.cos(np.linspace(0, 6, rows)),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            feature_path = Path(tmp) / "features.parquet"
            out_path = Path(tmp) / "events.jsonl"
            feat.to_parquet(feature_path, index=False)
            summary = run_replay_dry_run_v2(
                feature_parquet=feature_path,
                model_package=pkg,
                thresholds=thresholds,
                output_jsonl=out_path,
                mode="dual",
                limit=30,
                max_hold_minutes=4,
                confidence_buffer=0.05,
            )
            self.assertEqual(summary["rows_processed"], 30)
            self.assertTrue(out_path.exists())
            lines = out_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(lines), 0)
            events = [json.loads(x) for x in lines]
            event_types = {e.get("event_type") for e in events}
            self.assertIn("ENTRY", event_types)
            self.assertIn("EXIT", event_types)

            open_positions = 0
            for e in events:
                et = e.get("event_type")
                if et == "ENTRY":
                    open_positions += 1
                elif et == "EXIT":
                    open_positions -= 1
                self.assertGreaterEqual(open_positions, 0)
            self.assertEqual(open_positions, 0)

    def test_live_api_v2_state_persistence_with_stub_client(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)
        rows = 12
        ts = pd.date_range("2023-06-15 09:15:00", periods=rows, freq="min")
        feat = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": [str(t.date()) for t in ts],
                "feature_a": np.linspace(1.0, -1.0, rows),
                "feature_b": np.cos(np.linspace(0, 5, rows)),
            }
        ).to_dict(orient="records")

        class _StubClient:
            def __init__(self, *args, **kwargs):
                self._i = 0

            def build_latest_feature_row(self, instrument: str):
                row = dict(feat[min(self._i, len(feat) - 1)])
                row["timestamp"] = pd.Timestamp(row["timestamp"]).isoformat()
                self._i += 1
                return row

        original = lia.LiveMarketFeatureClient
        lia.LiveMarketFeatureClient = _StubClient
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_path = Path(tmp) / "live_v2_events.jsonl"
                summary = run_live_api_paper_loop_v2(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    output_jsonl=out_path,
                    mode="dual",
                    poll_seconds=0.01,
                    max_iterations=12,
                    max_hold_minutes=3,
                    confidence_buffer=0.05,
                )
                self.assertTrue(out_path.exists())
                self.assertGreater(summary["events_emitted"], 0)
                lines = out_path.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines if x.strip()]
                event_types = {e.get("event_type") for e in events}
                self.assertIn("ENTRY", event_types)
                self.assertIn("EXIT", event_types)
        finally:
            lia.LiveMarketFeatureClient = original

    def test_live_redis_v2_state_persistence_with_stub_pubsub(self) -> None:
        pkg = _build_model_package()
        thresholds = DecisionThresholds(ce=0.5, pe=0.5, cost_per_trade=0.0006)

        options_payload = {
            "instrument": "BANKNIFTY-I",
            "expiry": "2026-02-26",
            "pcr": 1.05,
            "strikes": [
                {
                    "strike": 47000,
                    "ce_ltp": 120.0,
                    "ce_oi": 150000,
                    "ce_volume": 4500,
                    "pe_ltp": 102.0,
                    "pe_oi": 130000,
                    "pe_volume": 4100,
                },
                {
                    "strike": 47100,
                    "ce_ltp": 95.0,
                    "ce_oi": 132000,
                    "ce_volume": 3800,
                    "pe_ltp": 126.0,
                    "pe_oi": 144000,
                    "pe_volume": 4600,
                },
                {
                    "strike": 47200,
                    "ce_ltp": 73.0,
                    "ce_oi": 121000,
                    "ce_volume": 3200,
                    "pe_ltp": 150.0,
                    "pe_oi": 159000,
                    "pe_volume": 5100,
                },
            ],
        }

        def _ohlc_msg(ts: str, close_px: float) -> dict:
            return {
                "type": "message",
                "channel": "market:ohlc:BANKNIFTY-I:1min",
                "data": json.dumps(
                    {
                        "stream": "Y1",
                        "event_time": ts,
                        "payload": {
                            "start_at": ts,
                            "open": close_px - 5.0,
                            "high": close_px + 5.0,
                            "low": close_px - 8.0,
                            "close": close_px,
                            "volume": 1500,
                            "oi": 2100,
                            "candle_closed": True,
                            "update_type": "candle",
                        },
                    }
                ),
            }

        messages = [
            {"type": "message", "channel": "market:options:BANKNIFTY-I", "data": json.dumps(options_payload)},
            _ohlc_msg("2023-06-15T09:15:00+05:30", 47050.0),
            _ohlc_msg("2023-06-15T09:16:00+05:30", 47080.0),
            _ohlc_msg("2023-06-15T09:17:00+05:30", 47020.0),
        ]

        class _StubPubSub:
            def __init__(self, rows):
                self._rows = list(rows)

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
                self._kv = {
                    "depth:BANKNIFTY-I:buy": json.dumps(
                        [{"price": 47049.5, "quantity": 1200}, {"price": 47049.0, "quantity": 900}]
                    ),
                    "depth:BANKNIFTY-I:sell": json.dumps(
                        [{"price": 47050.5, "quantity": 1000}, {"price": 47051.0, "quantity": 850}]
                    ),
                    "depth:BANKNIFTY-I:timestamp": "2023-06-15T09:17:00+05:30",
                    "depth:BANKNIFTY-I:total_bid_qty": "2100",
                    "depth:BANKNIFTY-I:total_ask_qty": "1850",
                }

            def pubsub(self, ignore_subscribe_messages=True):
                return self._pubsub

            def get(self, key):
                return self._kv.get(str(key))

        original_redis = lia.redis.Redis
        original_predict = lia.predict_decision_from_row
        state = {"idx": 0}

        def _stub_predict(row, model_package, thresholds, mode, **kwargs):
            ts = str(row.get("timestamp"))
            idx = state["idx"]
            state["idx"] = idx + 1
            action = "HOLD"
            ce_prob = 0.45
            pe_prob = 0.40
            if idx == 0:
                action = "BUY_CE"
                ce_prob = 0.81
                pe_prob = 0.14
            elif idx == 2:
                action = "BUY_PE"
                ce_prob = 0.20
                pe_prob = 0.77
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

        lia.redis.Redis = _StubRedis
        lia.predict_decision_from_row = _stub_predict
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_path = Path(tmp) / "live_redis_v2_events.jsonl"
                summary = run_live_redis_event_loop_v2(
                    instrument="BANKNIFTY-I",
                    model_package=pkg,
                    thresholds=thresholds,
                    output_jsonl=out_path,
                    mode="dual",
                    redis_host="localhost",
                    redis_port=6379,
                    redis_db=0,
                    max_iterations=3,
                    max_idle_seconds=0.01,
                    max_hold_minutes=5,
                    confidence_buffer=0.05,
                )
                self.assertTrue(out_path.exists())
                self.assertEqual(summary["bars_processed"], 3)
                lines = out_path.read_text(encoding="utf-8").strip().splitlines()
                events = [json.loads(x) for x in lines if x.strip()]
                event_types = {e.get("event_type") for e in events}
                self.assertIn("ENTRY", event_types)
                self.assertIn("MANAGE", event_types)
                self.assertIn("EXIT", event_types)
                for event in events:
                    self.assertEqual(event.get("source"), "redis_pubsub")
                    self.assertIn("depth", event)
                    self.assertGreater(float(event["depth"]["total_bid_qty"]), 0.0)
                    self.assertGreater(float(event["depth"]["total_ask_qty"]), 0.0)
                    self.assertTrue(np.isfinite(float(event["depth"]["imbalance"])))
        finally:
            lia.redis.Redis = original_redis
            lia.predict_decision_from_row = original_predict


if __name__ == "__main__":
    unittest.main()
