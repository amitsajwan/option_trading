import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

from contracts_app import build_snapshot_event
from contracts_app import build_strategy_position_event
from persistence_app.mongo_writer import SnapshotMongoWriter, StrategyMongoWriter
from strategy_app.contracts import PositionContext, SignalType, TradeSignal
from strategy_app.logging.signal_logger import SignalLogger


class _InsertResult:
    inserted_id = "stub"


class _UpdateResult:
    def __init__(self, *, matched_count: int, upserted_id: str | None) -> None:
        self.matched_count = matched_count
        self.upserted_id = upserted_id


class CollectionStub:
    def __init__(self) -> None:
        self.docs = []
        self.indexes = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _InsertResult()

    def update_one(self, filter_doc, update_doc, upsert=False):
        for existing in self.docs:
            if all(existing.get(key) == value for key, value in dict(filter_doc).items()):
                return _UpdateResult(matched_count=1, upserted_id=None)
        if bool(upsert):
            inserted = dict((update_doc or {}).get("$setOnInsert") or {})
            self.docs.append(inserted)
            return _UpdateResult(matched_count=0, upserted_id="stub")
        return _UpdateResult(matched_count=0, upserted_id=None)

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, dict(kwargs)))
        return f"idx_{len(self.indexes)}"


class DbStub(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = CollectionStub()
        return dict.__getitem__(self, key)


class MongoWriterTests(unittest.TestCase):
    def test_write_snapshot_event_is_idempotent_by_snapshot_id(self) -> None:
        writer = SnapshotMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = build_snapshot_event(
            snapshot={
                "snapshot_id": "snap-1",
                "instrument": "BANKNIFTY26MARFUT",
                "session_context": {"timestamp": "2026-03-12T09:30:00+05:30"},
            },
            source="snapshot_app",
            event_id="evt-snapshot-1",
        )

        with patch("persistence_app.mongo_writer.parse_snapshot_event", return_value=event):
            self.assertTrue(writer.write_snapshot_event({}))
            self.assertTrue(writer.write_snapshot_event({}))

        self.assertEqual(len(fake_db[writer.collection_name].docs), 1)

    def test_snapshot_writer_creates_unique_identity_indexes(self) -> None:
        writer = SnapshotMongoWriter()
        fake_db = DbStub()
        writer._db = fake_db

        writer._ensure_indexes()

        indexes = fake_db[writer.collection_name].indexes
        self.assertIn(
            (
                [("snapshot_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"snapshot_id": {"$exists": True, "$type": "string"}},
                },
            ),
            indexes,
        )
        self.assertIn(
            (
                [("event_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"event_id": {"$exists": True, "$type": "string"}},
                },
            ),
            indexes,
        )

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

    def test_write_trade_signal_event_is_idempotent_by_signal_id(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "event_id": "evt-signal-1",
            "signal": {
                "signal_id": "sig-1",
                "snapshot_id": "snap-1",
                "timestamp": "2026-03-12T09:30:00+05:30",
                "signal_type": "ENTRY",
                "direction": "CE",
            },
        }

        with patch("persistence_app.mongo_writer.parse_trade_signal_event", return_value=event):
            self.assertTrue(writer.write_trade_signal_event({}))
            self.assertTrue(writer.write_trade_signal_event({}))

        self.assertEqual(len(fake_db[writer.signal_collection_name].docs), 1)

    def test_write_strategy_vote_event_is_idempotent_by_snapshot_and_strategy(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "event_id": "evt-vote-1",
            "vote": {
                "snapshot_id": "snap-1",
                "strategy": "ORB",
                "trade_date": "2026-03-12",
                "timestamp": "2026-03-12T09:30:00+05:30",
                "signal_type": "ENTRY",
                "direction": "CE",
                "reason": "vote",
            },
        }
        duplicate = {
            **event,
            "event_id": "evt-vote-2",
        }

        with patch("persistence_app.mongo_writer.parse_strategy_vote_event", side_effect=[event, duplicate]):
            self.assertTrue(writer.write_strategy_vote_event({}))
            self.assertTrue(writer.write_strategy_vote_event({}))

        self.assertEqual(len(fake_db[writer.vote_collection_name].docs), 1)

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

    def test_write_strategy_position_event_is_idempotent_by_position_event_and_timestamp(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db_handle = lambda: fake_db  # type: ignore[method-assign]

        event = {
            "event_version": "1.0",
            "source": "strategy_app",
            "event_id": "evt-position-1",
            "position": {
                "position_id": "pos-1",
                "signal_id": "sig-1",
                "event": "POSITION_CLOSE",
                "timestamp": "2026-03-12T09:42:00+05:30",
                "direction": "CE",
                "reason": "exit",
                "pnl_pct": -0.04,
                "exit_reason": "STOP_LOSS",
            },
        }
        duplicate = {
            **event,
            "event_id": "evt-position-2",
        }

        with patch("persistence_app.mongo_writer.parse_strategy_position_event", side_effect=[event, duplicate]):
            self.assertTrue(writer.write_strategy_position_event({}))
            self.assertTrue(writer.write_strategy_position_event({}))

        self.assertEqual(len(fake_db[writer.position_collection_name].docs), 1)

    def test_strategy_writer_creates_unique_identity_indexes(self) -> None:
        writer = StrategyMongoWriter()
        fake_db = DbStub()
        writer._db = fake_db

        writer._ensure_indexes()

        vote_indexes = fake_db[writer.vote_collection_name].indexes
        signal_indexes = fake_db[writer.signal_collection_name].indexes
        position_indexes = fake_db[writer.position_collection_name].indexes

        self.assertIn(
            (
                [("snapshot_id", 1), ("strategy", 1), ("trade_date_ist", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {
                        "snapshot_id": {"$exists": True, "$type": "string"},
                        "strategy": {"$exists": True, "$type": "string"},
                        "trade_date_ist": {"$exists": True, "$type": "string"},
                    },
                },
            ),
            vote_indexes,
        )
        self.assertIn(
            (
                [("event_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"event_id": {"$exists": True, "$type": "string"}},
                },
            ),
            vote_indexes,
        )
        self.assertIn(
            (
                [("signal_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"signal_id": {"$exists": True, "$type": "string"}},
                },
            ),
            signal_indexes,
        )
        self.assertIn(
            (
                [("event_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"event_id": {"$exists": True, "$type": "string"}},
                },
            ),
            signal_indexes,
        )
        self.assertIn(
            (
                [("position_id", 1), ("event", 1), ("timestamp", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {
                        "position_id": {"$exists": True, "$type": "string"},
                        "event": {"$exists": True, "$type": "string"},
                        "timestamp": {"$exists": True, "$type": "string"},
                    },
                },
            ),
            position_indexes,
        )
        self.assertIn(
            (
                [("event_id", 1)],
                {
                    "unique": True,
                    "partialFilterExpression": {"event_id": {"$exists": True, "$type": "string"}},
                },
            ),
            position_indexes,
        )


if __name__ == "__main__":
    unittest.main()
