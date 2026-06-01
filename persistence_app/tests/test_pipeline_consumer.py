"""Unit tests for persistence_app/main_pipeline_consumer.py.

Tests pure helper functions (no Redis/MongoDB required) plus
an integration test of the consumer loop with mocked infrastructure.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from persistence_app.main_pipeline_consumer import (
    _build_doc,
    _decode_payload,
    _outcome,
    _stage_from_stream,
)


# ---------------------------------------------------------------------------
# _stage_from_stream
# ---------------------------------------------------------------------------

class StageFromStreamTests(unittest.TestCase):
    def test_sim_stream_name_extracts_slug(self) -> None:
        # stream:regime_decisions:sim:abc123  -> slug = "regime_decisions"
        self.assertEqual(_stage_from_stream("stream:regime_decisions:sim:abc123"), "regime")

    def test_execution_events_slug(self) -> None:
        self.assertEqual(_stage_from_stream("stream:execution_events:sim:run1"), "execution")

    def test_depth_decisions_slug(self) -> None:
        self.assertEqual(_stage_from_stream("stream:depth_decisions:sim:run1"), "depth")

    def test_unknown_slug_returns_slug_unchanged(self) -> None:
        self.assertEqual(_stage_from_stream("stream:custom_stage:sim:run1"), "custom_stage")

    def test_malformed_stream_falls_back_to_whole_name(self) -> None:
        result = _stage_from_stream("regime_only")
        # No colons — parts[1] would fail; falls back gracefully
        self.assertIsInstance(result, str)

    def test_all_known_slugs_mapped(self) -> None:
        slugs = [
            "regime_decisions", "entry_decisions", "direction_decisions",
            "depth_decisions",  "strike_decisions", "risk_decisions", "execution_events",
        ]
        expected = ["regime", "entry", "direction", "depth", "strike", "risk", "execution"]
        for slug, exp in zip(slugs, expected):
            self.assertEqual(_stage_from_stream(f"stream:{slug}:sim:run1"), exp,
                             f"slug={slug}")


# ---------------------------------------------------------------------------
# _outcome
# ---------------------------------------------------------------------------

class OutcomeTests(unittest.TestCase):
    def _p(self, **kw):
        return kw

    def test_regime_outcome(self) -> None:
        r = _outcome("regime", self._p(regime="TRENDING", confidence=0.88))
        self.assertIn("TRENDING", r)
        self.assertIn("0.88", r)

    def test_entry_allowed(self) -> None:
        self.assertEqual(_outcome("entry", self._p(allowed=True)),  "allowed")
        self.assertEqual(_outcome("entry", self._p(allowed=False)), "blocked")

    def test_direction_outcome(self) -> None:
        self.assertEqual(_outcome("direction", self._p(vetoed=True, direction="")),  "vetoed")
        self.assertEqual(_outcome("direction", self._p(vetoed=False, direction="CE")), "CE")

    def test_depth_blocked(self) -> None:
        self.assertEqual(_outcome("depth", self._p(proceed=False)), "blocked")

    def test_depth_aligned(self) -> None:
        r = _outcome("depth", self._p(proceed=True, depth_aligned=True, confidence=0.83))
        self.assertIn("aligned", r)
        self.assertIn("0.83", r)

    def test_depth_pass_not_aligned(self) -> None:
        r = _outcome("depth", self._p(proceed=True, depth_aligned=False, confidence=0.60))
        self.assertIn("pass", r)

    def test_strike_skipped(self) -> None:
        self.assertEqual(_outcome("strike", self._p(skipped=True)),  "skipped")

    def test_strike_value(self) -> None:
        self.assertEqual(_outcome("strike", self._p(skipped=False, strike=49500)), "49500")

    def test_risk_approved(self) -> None:
        r = _outcome("risk", self._p(approved=True, approved_lots=2))
        self.assertIn("approved", r)
        self.assertIn("2L", r)

    def test_risk_rejected(self) -> None:
        self.assertEqual(_outcome("risk", self._p(approved=False)), "rejected")

    def test_execution_signal_type(self) -> None:
        self.assertEqual(_outcome("execution", self._p(signal_type="ENTER")), "ENTER")
        self.assertEqual(_outcome("execution", self._p(signal_type="SKIP")),  "SKIP")

    def test_unknown_stage_returns_empty(self) -> None:
        self.assertEqual(_outcome("unknown_stage", {}), "")

    def test_raises_do_not_propagate(self) -> None:
        # Bad types should return "" not raise
        result = _outcome("regime", self._p(regime=None, confidence="not-a-float"))
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# _decode_payload
# ---------------------------------------------------------------------------

class DecodePayloadTests(unittest.TestCase):
    def test_valid_json_decoded(self) -> None:
        fields = {"payload": json.dumps({"trace_id": "t1", "regime": "TRENDING"})}
        result = _decode_payload(fields)
        self.assertIsNotNone(result)
        self.assertEqual(result["trace_id"], "t1")

    def test_missing_key_returns_none(self) -> None:
        self.assertIsNone(_decode_payload({}))
        self.assertIsNone(_decode_payload(None))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(_decode_payload({"payload": ""}))
        self.assertIsNone(_decode_payload({"payload": "  "}))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(_decode_payload({"payload": "{not-json}"}))

    def test_non_dict_json_returns_none(self) -> None:
        self.assertIsNone(_decode_payload({"payload": json.dumps([1, 2, 3])}))

    def test_nested_payload_preserved(self) -> None:
        inner = {"trace_id": "t1", "evidence": {"key": "val"}}
        result = _decode_payload({"payload": json.dumps(inner)})
        self.assertEqual(result["evidence"]["key"], "val")


# ---------------------------------------------------------------------------
# _build_doc
# ---------------------------------------------------------------------------

class BuildDocTests(unittest.TestCase):
    def _regime_payload(self) -> dict:
        return {
            "trace_id": "trace-001",
            "run_id": "run-001",
            "event_id": "evt-001",
            "parent_event_id": "parent-001",
            "event_type": "regime_decision",
            "confidence": 0.88,
            "plugin_id": "regime_classifier_v1",
            "plugin_version": "1.0",
            "parity_mode": "live_full",
            "timestamp": "2026-05-31T10:00:00+05:30",
            "regime": "TRENDING",
        }

    def test_required_fields_present(self) -> None:
        received_at = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)
        doc = _build_doc("regime", self._regime_payload(), received_at)
        for field in ["trace_id", "run_id", "stage", "event_id", "event_type",
                      "confidence", "outcome", "plugin_id", "plugin_version",
                      "parity_mode", "timestamp", "_received_at", "payload"]:
            self.assertIn(field, doc, f"field {field!r} missing from doc")

    def test_stage_set_correctly(self) -> None:
        doc = _build_doc("regime", self._regime_payload(),
                         datetime.now(timezone.utc))
        self.assertEqual(doc["stage"], "regime")

    def test_received_at_is_datetime(self) -> None:
        received_at = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)
        doc = _build_doc("regime", self._regime_payload(), received_at)
        self.assertEqual(doc["_received_at"], received_at)

    def test_outcome_computed(self) -> None:
        doc = _build_doc("regime", self._regime_payload(),
                         datetime.now(timezone.utc))
        self.assertIn("TRENDING", doc["outcome"])

    def test_payload_stored_intact(self) -> None:
        p = self._regime_payload()
        doc = _build_doc("regime", p, datetime.now(timezone.utc))
        self.assertEqual(doc["payload"]["regime"], "TRENDING")

    def test_entry_doc_outcome(self) -> None:
        p = {**self._regime_payload(), "event_type": "entry_decision", "allowed": True}
        doc = _build_doc("entry", p, datetime.now(timezone.utc))
        self.assertEqual(doc["outcome"], "allowed")

    def test_execution_doc_outcome(self) -> None:
        p = {**self._regime_payload(), "event_type": "execution", "signal_type": "ENTER"}
        doc = _build_doc("execution", p, datetime.now(timezone.utc))
        self.assertEqual(doc["outcome"], "ENTER")

    def test_missing_trace_id_stored_as_empty_string(self) -> None:
        p = deepcopy(self._regime_payload())
        del p["trace_id"]
        doc = _build_doc("regime", p, datetime.now(timezone.utc))
        self.assertEqual(doc["trace_id"], "")


# ---------------------------------------------------------------------------
# Integration: consumer loop processes messages and writes to MongoDB
# ---------------------------------------------------------------------------

class ConsumerLoopIntegrationTests(unittest.TestCase):
    """
    Runs run_loop for a short time with mocked Redis + MongoDB.
    Verifies that decoded events are written as documents.
    """

    def _make_entry(self, trace_id: str, stage: str) -> tuple:
        payload = {
            "trace_id": trace_id,
            "run_id": "run-test",
            "event_id": "e1",
            "parent_event_id": "",
            "event_type": f"{stage}_decision",
            "confidence": 0.75,
            "plugin_id": f"{stage}_plugin",
            "plugin_version": "1.0",
            "parity_mode": "live_full",
            "timestamp": "2026-05-31T10:00:00+05:30",
            "regime": "TRENDING" if stage == "regime" else "",
            "allowed": True,
            "signal_type": "ENTER" if stage == "execution" else "SKIP",
        }
        return (f"stream:{'regime' if stage == 'regime' else stage}_decisions:sim:run-test",
                [("1748671200000-0", {"payload": json.dumps(payload)})])

    def test_consumer_writes_docs_to_mongodb(self) -> None:
        from persistence_app.main_pipeline_consumer import run_loop

        written: list[dict] = []
        acked: list = []

        # --- Fake Redis ---
        call_count = [0]

        def fake_xreadgroup(group, consumer, streams, count, block):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: return regime + execution events
                stream1 = "stream:regime_decisions:sim:run-test"
                stream2 = "stream:execution_events:sim:run-test"
                payload1 = json.dumps({
                    "trace_id": "tid1", "run_id": "run-test",
                    "event_id": "e1", "parent_event_id": "",
                    "event_type": "regime_decision", "confidence": 0.85,
                    "plugin_id": "regime_v1", "plugin_version": "1.0",
                    "parity_mode": "live_full",
                    "timestamp": "2026-05-31T10:00:00+05:30",
                    "regime": "TRENDING",
                })
                payload2 = json.dumps({
                    "trace_id": "tid1", "run_id": "run-test",
                    "event_id": "e2", "parent_event_id": "e1",
                    "event_type": "execution", "confidence": 0.85,
                    "plugin_id": "exec_v1", "plugin_version": "1.0",
                    "parity_mode": "live_full",
                    "timestamp": "2026-05-31T10:00:01+05:30",
                    "signal_type": "ENTER",
                })
                return [
                    (stream1, [("1000-0", {"payload": payload1})]),
                    (stream2, [("1001-0", {"payload": payload2})]),
                ]
            # Signal stop via KeyboardInterrupt on second call
            raise KeyboardInterrupt

        fake_redis = MagicMock()
        fake_redis.xreadgroup.side_effect = fake_xreadgroup
        fake_redis.xack = MagicMock(side_effect=lambda s, g, *ids: acked.extend(ids))

        # --- Fake MongoDB collection ---
        fake_coll = MagicMock()
        fake_coll.insert_many = MagicMock(side_effect=lambda docs, **kw: written.extend(docs))
        fake_coll.create_index = MagicMock()

        with patch("persistence_app.main_pipeline_consumer._redis_client",
                   return_value=fake_redis), \
             patch("persistence_app.main_pipeline_consumer._ensure_group"), \
             patch("persistence_app.main_pipeline_consumer._get_collection",
                   return_value=fake_coll), \
             patch.dict("os.environ", {"SIM_RUN_ID": "run-test"}, clear=False):

            exit_code = run_loop(run_id="run-test", health_log_interval_sec=9999)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(written), 2, f"Expected 2 docs written, got {len(written)}: {written}")
        stages = {d["stage"] for d in written}
        self.assertIn("regime",    stages)
        self.assertIn("execution", stages)
        self.assertTrue(all(d["trace_id"] == "tid1" for d in written))

    def test_consumer_skips_messages_without_trace_id(self) -> None:
        from persistence_app.main_pipeline_consumer import run_loop

        written: list[dict] = []
        call_count = [0]

        def fake_xreadgroup(group, consumer, streams, count, block):
            call_count[0] += 1
            if call_count[0] == 1:
                stream = "stream:regime_decisions:sim:run-test"
                no_tid = json.dumps({"event_type": "regime_decision", "run_id": "run-test"})
                return [(stream, [("1000-0", {"payload": no_tid})])]
            raise KeyboardInterrupt

        fake_redis = MagicMock()
        fake_redis.xreadgroup.side_effect = fake_xreadgroup
        fake_redis.xack = MagicMock()

        fake_coll = MagicMock()
        fake_coll.insert_many = MagicMock(side_effect=lambda docs, **kw: written.extend(docs))
        fake_coll.create_index = MagicMock()

        with patch("persistence_app.main_pipeline_consumer._redis_client",
                   return_value=fake_redis), \
             patch("persistence_app.main_pipeline_consumer._ensure_group"), \
             patch("persistence_app.main_pipeline_consumer._get_collection",
                   return_value=fake_coll):
            run_loop(run_id="run-test", health_log_interval_sec=9999)

        self.assertEqual(len(written), 0, "Message without trace_id should be skipped")

    def test_consumer_acknowledges_all_messages(self) -> None:
        from persistence_app.main_pipeline_consumer import run_loop

        acked_ids: list = []
        call_count = [0]

        def fake_xreadgroup(group, consumer, streams, count, block):
            call_count[0] += 1
            if call_count[0] == 1:
                stream = "stream:regime_decisions:sim:run-test"
                payload = json.dumps({
                    "trace_id": "tid2", "run_id": "r", "event_id": "e1",
                    "parent_event_id": "", "event_type": "regime_decision",
                    "confidence": 0.5, "plugin_id": "p", "plugin_version": "1",
                    "parity_mode": "live_full",
                    "timestamp": "2026-05-31T10:00:00+05:30",
                    "regime": "SIDEWAYS",
                })
                return [(stream, [("2000-0", {"payload": payload})])]
            raise KeyboardInterrupt

        fake_redis = MagicMock()
        fake_redis.xreadgroup.side_effect = fake_xreadgroup
        fake_redis.xack = MagicMock(side_effect=lambda s, g, *ids: acked_ids.extend(ids))

        fake_coll = MagicMock()
        fake_coll.insert_many = MagicMock()
        fake_coll.create_index = MagicMock()

        with patch("persistence_app.main_pipeline_consumer._redis_client",
                   return_value=fake_redis), \
             patch("persistence_app.main_pipeline_consumer._ensure_group"), \
             patch("persistence_app.main_pipeline_consumer._get_collection",
                   return_value=fake_coll):
            run_loop(run_id="run-test", health_log_interval_sec=9999)

        self.assertIn("2000-0", acked_ids, "Message should be ACKed even when trace_id missing")


if __name__ == "__main__":
    unittest.main()
