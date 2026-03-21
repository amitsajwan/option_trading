import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

from contracts_app import build_strategy_position_event
from persistence_app.mongo_writer import StrategyMongoWriter
from strategy_app.contracts import PositionContext, SignalType, TradeSignal
from strategy_app.logging.signal_logger import SignalLogger


class _InsertResult:
    inserted_id = "stub"


class CollectionStub:
    def __init__(self) -> None:
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _InsertResult()


class DbStub(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = CollectionStub()
        return dict.__getitem__(self, key)


class MongoWriterTests(unittest.TestCase):
    def test_write_trade_signal_event_flattens_ml_metrics(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "signal": {
                "signal_id": "sig-1",
                "snapshot_id": "snap-1",
                "timestamp": "2026-03-12T09:30:00+05:30",
                "regime": "TRENDING",
                "signal_type": "ENTRY",
                "direction": "CE",
                "confidence": 0.72,
                "reason": "ml_pure_entry",
                "engine_mode": "ml_pure",
                "decision_mode": "ml_staged",
                "decision_metrics": {
                    "entry_prob": 0.68,
                    "direction_up_prob": 0.73,
                    "recipe_prob": 0.81,
                    "recipe_margin": 0.09,
                },
            },
        }

        with patch("persistence_app.mongo_writer.parse_trade_signal_event", return_value=event):
            written = writer.write_trade_signal_event({})

        self.assertTrue(written)
        doc = fake_db[writer.signal_collection_name].docs[0]
        self.assertIn("decision_metrics", doc)
        self.assertAlmostEqual(float(doc["ml_entry_prob"]), 0.68, places=6)
        self.assertAlmostEqual(float(doc["ml_direction_up_prob"]), 0.73, places=6)
        self.assertAlmostEqual(float(doc["ml_ce_prob"]), 0.73, places=6)
        self.assertAlmostEqual(float(doc["ml_pe_prob"]), 0.27, places=6)

    def test_write_strategy_position_event_derives_actual_outcome(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "position": {
                "position_id": "pos-1",
                "signal_id": "sig-1",
                "event": "POSITION_CLOSE",
                "timestamp": "2026-03-12T09:42:00+05:30",
                "direction": "CE",
                "reason": "exit",
                "engine_mode": "ml_pure",
                "pnl_pct": -0.04,
                "exit_reason": "STOP_LOSS",
                "decision_metrics": {"entry_prob": 0.68},
            },
        }

        with patch("persistence_app.mongo_writer.parse_strategy_position_event", return_value=event):
            written = writer.write_strategy_position_event({})

        self.assertTrue(written)
        doc = fake_db[writer.position_collection_name].docs[0]
        self.assertEqual(doc["actual_outcome"], "stop")
        self.assertAlmostEqual(float(doc["actual_return_pct"]), -0.04, places=6)
        self.assertAlmostEqual(float(doc["ml_entry_prob"]), 0.68, places=6)

    def test_write_strategy_position_event_falls_back_to_metadata_signal_id(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "metadata": {"signal_id": "sig-meta-1"},
            "position": {
                "position_id": "pos-1",
                "event": "POSITION_CLOSE",
                "timestamp": "2026-03-12T09:42:00+05:30",
                "direction": "CE",
                "reason": "exit",
                "engine_mode": "ml_pure",
                "pnl_pct": -0.04,
                "exit_reason": "STOP_LOSS",
                "decision_metrics": {"entry_prob": 0.68},
            },
        }

        with patch("persistence_app.mongo_writer.parse_strategy_position_event", return_value=event):
            written = writer.write_strategy_position_event({})

        self.assertTrue(written)
        doc = fake_db[writer.position_collection_name].docs[0]
        self.assertEqual(doc["signal_id"], "sig-meta-1")

    def test_write_strategy_position_event_preserves_logger_entry_metrics(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"STRATEGY_REDIS_PUBLISH_ENABLED": "0"}, clear=False):
                root = Path(tmpdir)
                logger = SignalLogger(root)
                logger.set_run_context(
                    "run-1",
                    {
                        "engine_mode": "ml_pure",
                        "strategy_family_version": "ML_PURE_STAGED_V1",
                        "strategy_profile_id": "ml_pure_staged_v1",
                    },
                )
                signal = TradeSignal(
                    signal_id="sig-logger-1",
                    timestamp=datetime(2026, 3, 12, 4, 0, tzinfo=timezone.utc),
                    snapshot_id="snap-1",
                    signal_type=SignalType.ENTRY,
                    direction="CE",
                    strike=60000,
                    entry_premium=100.0,
                    source="ML_PURE",
                    confidence=0.72,
                    reason="ml_pure_staged: entry",
                    decision_metrics={
                        "entry_prob": 0.68,
                        "direction_up_prob": 0.73,
                        "recipe_prob": 0.81,
                        "recipe_margin": 0.09,
                    },
                )
                position = PositionContext(
                    position_id="pos-logger-1",
                    direction="CE",
                    strike=60000,
                    expiry=None,
                    entry_premium=100.0,
                    entry_time=datetime(2026, 3, 12, 4, 0, tzinfo=timezone.utc),
                    entry_snapshot_id="snap-1",
                    lots=1,
                    decision_metrics=dict(signal.decision_metrics),
                    engine_mode="ml_pure",
                    decision_mode="ml_staged",
                    strategy_family_version="ML_PURE_STAGED_V1",
                    strategy_profile_id="ml_pure_staged_v1",
                )

                logger.log_position_open(signal, position)
                position_row = json.loads((root / "positions.jsonl").read_text(encoding="utf-8").splitlines()[0])
                event = build_strategy_position_event(
                    position=position_row,
                    source="strategy_app",
                    metadata={"run_id": "run-1", "position_id": "pos-logger-1", "signal_id": "sig-logger-1"},
                )

        written = writer.write_strategy_position_event(event)

        self.assertTrue(written)
        doc = fake_db[writer.position_collection_name].docs[0]
        self.assertAlmostEqual(float(doc["ml_entry_prob"]), 0.68, places=6)
        self.assertAlmostEqual(float(doc["ml_direction_up_prob"]), 0.73, places=6)
        self.assertAlmostEqual(float(doc["ml_ce_prob"]), 0.73, places=6)
        self.assertAlmostEqual(float(doc["ml_pe_prob"]), 0.27, places=6)
        self.assertAlmostEqual(float(doc["ml_recipe_prob"]), 0.81, places=6)
        self.assertAlmostEqual(float(doc["ml_recipe_margin"]), 0.09, places=6)


if __name__ == "__main__":
    unittest.main()
