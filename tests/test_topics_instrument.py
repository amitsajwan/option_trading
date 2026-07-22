"""Instrument-scoping of contracts_app.topics (live/oos pubsub topic names).

Guarantees:
  - primary instrument (BANKNIFTY / unset) -> legacy topic strings unchanged
  - secondary instrument (NIFTY) -> slug inserted after ``market:``
  - explicit per-topic env override always wins verbatim
"""
from __future__ import annotations

import os
import unittest

import contracts_app.topics as topics


class _EnvGuard:
    """Save/restore the env keys these tests mutate."""

    _KEYS = [
        "STRATEGY_INSTRUMENT",
        "SNAPSHOT_V1_TOPIC",
        "LIVE_TOPIC",
        "HISTORICAL_TOPIC",
        "STRATEGY_VOTE_TOPIC",
    ]

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestTopicScoping(unittest.TestCase):
    def test_primary_unset_is_legacy(self) -> None:
        with _EnvGuard():
            self.assertEqual(topics.snapshot_topic(), "market:snapshot:v1")
            self.assertEqual(topics.strategy_vote_topic(), "market:strategy:votes:v1")
            self.assertEqual(topics.strategy_decision_trace_topic(),
                             "market:strategy:decision_trace:v1")
            self.assertEqual(topics.historical_snapshot_topic(),
                             "market:snapshot:v1:historical")

    def test_banknifty_explicit_is_legacy(self) -> None:
        with _EnvGuard():
            os.environ["STRATEGY_INSTRUMENT"] = "BANKNIFTY"
            self.assertEqual(topics.snapshot_topic(), "market:snapshot:v1")
            self.assertEqual(topics.trade_signal_topic(), "market:strategy:signals:v1")

    def test_nifty_scoped(self) -> None:
        with _EnvGuard():
            os.environ["STRATEGY_INSTRUMENT"] = "NIFTY"
            self.assertEqual(topics.snapshot_topic(), "market:nifty:snapshot:v1")
            self.assertEqual(topics.strategy_vote_topic(),
                             "market:nifty:strategy:votes:v1")
            self.assertEqual(topics.historical_snapshot_topic(),
                             "market:nifty:snapshot:v1:historical")
            self.assertEqual(topics.execution_events_topic(),
                             "market:nifty:strategy:execution_events:v1")

    def test_explicit_override_wins(self) -> None:
        with _EnvGuard():
            os.environ["STRATEGY_INSTRUMENT"] = "NIFTY"
            os.environ["SNAPSHOT_V1_TOPIC"] = "custom:topic"
            self.assertEqual(topics.snapshot_topic(), "custom:topic")

    def test_nifty_and_banknifty_topics_distinct(self) -> None:
        with _EnvGuard():
            os.environ["STRATEGY_INSTRUMENT"] = "BANKNIFTY"
            bn = topics.snapshot_topic()
            os.environ["STRATEGY_INSTRUMENT"] = "NIFTY"
            nf = topics.snapshot_topic()
            self.assertNotEqual(bn, nf)


class TestStreamNameForTopic(unittest.TestCase):
    """Regression (2026-07-22): the Streams name must derive from the SAME
    instrument-scoped topic string as the pub/sub path, or two instruments
    running streams transport at once silently share one stream. Caught
    live during a NIFTY rehearsal deploy, before it reached BankNifty."""

    def test_primary_live(self) -> None:
        self.assertEqual(topics.stream_name_for_topic("market:snapshot:v1"),
                          "stream:snapshots:live")

    def test_primary_historical(self) -> None:
        self.assertEqual(topics.stream_name_for_topic("market:snapshot:v1:historical"),
                          "stream:snapshots:historical")

    def test_secondary_live(self) -> None:
        self.assertEqual(topics.stream_name_for_topic("market:nifty:snapshot:v1"),
                          "stream:snapshots:nifty:live")

    def test_secondary_historical(self) -> None:
        self.assertEqual(
            topics.stream_name_for_topic("market:nifty:snapshot:v1:historical"),
            "stream:snapshots:nifty:historical",
        )

    def test_matches_live_pubsub_topic_derivation(self) -> None:
        """Feed real snapshot_topic() output through the deriver for both
        instruments -- ties this test to the actual topic-generation path,
        not just hand-written literal strings."""
        with _EnvGuard():
            os.environ["STRATEGY_INSTRUMENT"] = "BANKNIFTY"
            bn_stream = topics.stream_name_for_topic(topics.snapshot_topic())
            os.environ["STRATEGY_INSTRUMENT"] = "NIFTY"
            nf_stream = topics.stream_name_for_topic(topics.snapshot_topic())
        self.assertNotEqual(bn_stream, nf_stream)
        self.assertEqual(bn_stream, "stream:snapshots:live")
        self.assertEqual(nf_stream, "stream:snapshots:nifty:live")


if __name__ == "__main__":
    unittest.main()
