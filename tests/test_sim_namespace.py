"""Tests for contracts_app.sim_namespace — SIM-1 foundation.

These tests pin the interface contract that every other SIM-* story
imports from. Loosen them ONLY by also updating the design doc
(memory/project_sim_replay_design_2026-05-27) and the scrum-board
story (docs/SCRUM_BOARD_SIM_REPLAY.md, SIM-1 section).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from contracts_app import Namespace, resolve_namespace


class TestResolveNamespace(unittest.TestCase):
    def test_live_no_run_id_required(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(ns.kind, "live")
        self.assertIsNone(ns.run_id)

    def test_oos_no_run_id_required(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(ns.kind, "oos")
        self.assertIsNone(ns.run_id)

    def test_oos_ignores_run_id(self) -> None:
        # OOS uses run_id from eval API at write time, but the namespace
        # resolution itself does not encode it (collection / topic names are
        # the same regardless of which OOS run is reading them).
        ns = resolve_namespace("oos", run_id="should-be-ignored")
        self.assertIsNone(ns.run_id)

    def test_sim_requires_run_id(self) -> None:
        with self.assertRaises(ValueError):
            resolve_namespace("sim")

    def test_sim_empty_run_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_namespace("sim", run_id="")

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_namespace("paper")  # type: ignore[arg-type]


class TestCollectionFor(unittest.TestCase):
    def test_live_returns_base_unchanged(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(
            ns.collection_for("phase1_market_snapshots"),
            "phase1_market_snapshots",
        )
        self.assertEqual(ns.collection_for("strategy_votes"), "strategy_votes")

    def test_oos_suffixes_historical(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(
            ns.collection_for("phase1_market_snapshots"),
            "phase1_market_snapshots_historical",
        )
        self.assertEqual(
            ns.collection_for("strategy_decision_traces"),
            "strategy_decision_traces_historical",
        )

    def test_sim_suffixes_sim(self) -> None:
        ns = resolve_namespace("sim", run_id="r123")
        self.assertEqual(
            ns.collection_for("phase1_market_snapshots"),
            "phase1_market_snapshots_sim",
        )
        self.assertEqual(
            ns.collection_for("market_depth_ticks"),
            "market_depth_ticks_sim",
        )

    def test_non_namespaced_collection_passes_through(self) -> None:
        # strategy_eval_runs is shared across kinds (with a `kind` field
        # disambiguating rows) — must not be suffixed.
        for kind in ("live", "oos"):
            ns = resolve_namespace(kind)  # type: ignore[arg-type]
            self.assertEqual(
                ns.collection_for("strategy_eval_runs"),
                "strategy_eval_runs",
            )
        ns_sim = resolve_namespace("sim", run_id="r1")
        self.assertEqual(
            ns_sim.collection_for("strategy_eval_runs"),
            "strategy_eval_runs",
        )

    def test_empty_base_rejected(self) -> None:
        ns = resolve_namespace("live")
        with self.assertRaises(ValueError):
            ns.collection_for("")
        with self.assertRaises(ValueError):
            ns.collection_for("   ")


class TestStreamFor(unittest.TestCase):
    def test_live_is_pubsub_topic(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(ns.stream_for("snapshots"), "market:snapshots:v1")
        self.assertEqual(ns.stream_for("votes"), "market:votes:v1")

    def test_oos_is_historical_pubsub_topic(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(
            ns.stream_for("snapshots"),
            "market:snapshots:v1:historical",
        )

    def test_sim_is_per_run_stream(self) -> None:
        ns = resolve_namespace("sim", run_id="018f7a")
        self.assertEqual(
            ns.stream_for("snapshots"),
            "stream:snapshots:sim:018f7a",
        )
        self.assertEqual(
            ns.stream_for("decision_trace"),
            "stream:decision_trace:sim:018f7a",
        )

    def test_sim_streams_isolated_per_run(self) -> None:
        a = resolve_namespace("sim", run_id="run-a")
        b = resolve_namespace("sim", run_id="run-b")
        self.assertNotEqual(a.stream_for("snapshots"), b.stream_for("snapshots"))

    def test_empty_what_rejected(self) -> None:
        ns = resolve_namespace("live")
        with self.assertRaises(ValueError):
            ns.stream_for("")


class TestStateKeyFor(unittest.TestCase):
    def test_live_prefix(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(ns.state_key_for("depth:atm_ce:latest"), "live:depth:atm_ce:latest")

    def test_oos_prefix(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(
            ns.state_key_for("depth:atm_ce:latest"),
            "historical:depth:atm_ce:latest",
        )

    def test_sim_prefix_includes_run_id(self) -> None:
        ns = resolve_namespace("sim", run_id="rrr")
        self.assertEqual(
            ns.state_key_for("depth:atm_ce:latest"),
            "sim:rrr:depth:atm_ce:latest",
        )

    def test_parallel_sim_runs_have_distinct_state_keys(self) -> None:
        a = resolve_namespace("sim", run_id="a")
        b = resolve_namespace("sim", run_id="b")
        self.assertNotEqual(
            a.state_key_for("depth:atm_pe:latest"),
            b.state_key_for("depth:atm_pe:latest"),
        )

    def test_empty_key_rejected(self) -> None:
        ns = resolve_namespace("live")
        with self.assertRaises(ValueError):
            ns.state_key_for("")


class TestRunDirFor(unittest.TestCase):
    def test_live_path(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(ns.run_dir_for(), Path("/app/.run/strategy_app"))

    def test_oos_path(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(ns.run_dir_for(), Path("/app/.run/strategy_app_historical"))

    def test_sim_path_includes_run_id(self) -> None:
        ns = resolve_namespace("sim", run_id="018f7abc")
        self.assertEqual(
            ns.run_dir_for(),
            Path("/app/.run/strategy_app_sim/018f7abc"),
        )

    def test_sim_paths_isolated_per_run(self) -> None:
        a = resolve_namespace("sim", run_id="aaa")
        b = resolve_namespace("sim", run_id="bbb")
        self.assertNotEqual(a.run_dir_for(), b.run_dir_for())


class TestLockKeyFor(unittest.TestCase):
    def test_live_lock_present(self) -> None:
        ns = resolve_namespace("live")
        self.assertEqual(
            ns.lock_key_for(),
            "strategy_app:consumer_lock:market:snapshot:v1",
        )

    def test_oos_lock_present(self) -> None:
        ns = resolve_namespace("oos")
        self.assertEqual(
            ns.lock_key_for(),
            "strategy_app_historical:consumer_lock:market:snapshot:v1:historical",
        )

    def test_sim_lock_is_none_by_design(self) -> None:
        """Sim runs use ephemeral consumer containers + Redis Streams
        consumer groups; consumer locks are unnecessary AND were the source
        of the 2026-05-27 morning's stale-lock crash loop. This is a
        deliberate design choice — keep this test green forever."""
        ns = resolve_namespace("sim", run_id="rrr")
        self.assertIsNone(ns.lock_key_for())


class TestTransport(unittest.TestCase):
    def test_live_pubsub(self) -> None:
        self.assertEqual(resolve_namespace("live").transport(), "pubsub")

    def test_oos_pubsub(self) -> None:
        self.assertEqual(resolve_namespace("oos").transport(), "pubsub")

    def test_sim_streams(self) -> None:
        self.assertEqual(
            resolve_namespace("sim", run_id="r1").transport(),
            "streams",
        )


class TestNamespaceImmutable(unittest.TestCase):
    def test_namespace_is_frozen(self) -> None:
        ns = resolve_namespace("sim", run_id="r1")
        with self.assertRaises((AttributeError, Exception)):
            ns.kind = "live"  # type: ignore[misc]


class TestKindsAreDistinct(unittest.TestCase):
    """Spec-level guard: the three kinds MUST produce distinct names for
    every base resource. If a refactor accidentally collapses two kinds,
    these tests catch it."""

    def test_collections_distinct_across_kinds(self) -> None:
        bases = [
            "phase1_market_snapshots",
            "strategy_votes",
            "trade_signals",
            "strategy_positions",
            "strategy_decision_traces",
            "market_depth_ticks",
        ]
        live = resolve_namespace("live")
        oos = resolve_namespace("oos")
        sim = resolve_namespace("sim", run_id="r1")
        for base in bases:
            collections = {
                live.collection_for(base),
                oos.collection_for(base),
                sim.collection_for(base),
            }
            self.assertEqual(
                len(collections),
                3,
                f"kinds collide on collection {base!r}: {collections}",
            )

    def test_streams_distinct_across_kinds(self) -> None:
        live = resolve_namespace("live").stream_for("snapshots")
        oos = resolve_namespace("oos").stream_for("snapshots")
        sim = resolve_namespace("sim", run_id="r1").stream_for("snapshots")
        self.assertEqual(len({live, oos, sim}), 3)

    def test_state_keys_distinct_across_kinds(self) -> None:
        live = resolve_namespace("live").state_key_for("k")
        oos = resolve_namespace("oos").state_key_for("k")
        sim = resolve_namespace("sim", run_id="r1").state_key_for("k")
        self.assertEqual(len({live, oos, sim}), 3)

    def test_run_dirs_distinct_across_kinds(self) -> None:
        live = resolve_namespace("live").run_dir_for()
        oos = resolve_namespace("oos").run_dir_for()
        sim = resolve_namespace("sim", run_id="r1").run_dir_for()
        self.assertEqual(len({live, oos, sim}), 3)


class TestInstrumentParity(unittest.TestCase):
    """The instrument axis MUST be backward-compatible: the primary instrument
    (BANKNIFTY) — whether passed explicitly, omitted, empty, or lower-case —
    produces names byte-identical to the pre-instrument-axis era. If this ever
    fails, existing live/sim collections, topics, run-dirs and locks have been
    orphaned. Keep green forever."""

    _BASES = [
        "phase1_market_snapshots",
        "strategy_votes",
        "trade_signals",
        "strategy_positions",
        "strategy_decision_traces",
        "market_depth_ticks",
    ]

    def _assert_same_surface(self, a, b) -> None:
        for base in self._BASES:
            self.assertEqual(a.collection_for(base), b.collection_for(base))
        for what in ("snapshot", "snapshots", "votes", "decision_trace"):
            self.assertEqual(a.stream_for(what), b.stream_for(what))
        self.assertEqual(a.state_key_for("depth:atm_ce:latest"),
                         b.state_key_for("depth:atm_ce:latest"))
        self.assertEqual(a.run_dir_for(), b.run_dir_for())
        self.assertEqual(a.lock_key_for(), b.lock_key_for())

    def test_primary_default_matches_explicit_banknifty(self) -> None:
        for kind, run_id in (("live", None), ("oos", None), ("sim", "r1")):
            default = resolve_namespace(kind, run_id=run_id)  # type: ignore[arg-type]
            explicit = resolve_namespace(kind, run_id=run_id, instrument="BANKNIFTY")  # type: ignore[arg-type]
            self._assert_same_surface(default, explicit)

    def test_primary_accepts_none_empty_and_lowercase(self) -> None:
        live = resolve_namespace("live")
        for variant in (None, "", "   ", "banknifty", "BankNifty"):
            self._assert_same_surface(live, resolve_namespace("live", instrument=variant))

    def test_primary_live_legacy_names_unchanged(self) -> None:
        ns = resolve_namespace("live", instrument="BANKNIFTY")
        self.assertEqual(ns.collection_for("strategy_positions"), "strategy_positions")
        self.assertEqual(ns.stream_for("snapshot"), "market:snapshot:v1")
        self.assertEqual(ns.state_key_for("depth:atm_ce:latest"), "live:depth:atm_ce:latest")
        self.assertEqual(ns.run_dir_for(), Path("/app/.run/strategy_app"))
        self.assertEqual(ns.lock_key_for(),
                         "strategy_app:consumer_lock:market:snapshot:v1")

    def test_primary_sim_legacy_names_unchanged(self) -> None:
        ns = resolve_namespace("sim", run_id="r123", instrument="BANKNIFTY")
        self.assertEqual(ns.collection_for("phase1_market_snapshots"),
                         "phase1_market_snapshots_sim")
        self.assertEqual(ns.stream_for("snapshots"), "stream:snapshots:sim:r123")
        self.assertEqual(ns.state_key_for("k"), "sim:r123:k")
        self.assertEqual(ns.run_dir_for(), Path("/app/.run/strategy_app_sim/r123"))


class TestSecondaryInstrument(unittest.TestCase):
    """NIFTY (a secondary instrument) inserts its slug into every name."""

    def test_nifty_live_collections(self) -> None:
        ns = resolve_namespace("live", instrument="NIFTY")
        self.assertEqual(ns.collection_for("strategy_positions"), "strategy_positions_nifty")
        self.assertEqual(ns.collection_for("phase1_market_snapshots"),
                         "phase1_market_snapshots_nifty")

    def test_nifty_sim_collections(self) -> None:
        ns = resolve_namespace("sim", run_id="r1", instrument="NIFTY")
        self.assertEqual(ns.collection_for("strategy_positions"),
                         "strategy_positions_nifty_sim")

    def test_nifty_oos_collections(self) -> None:
        ns = resolve_namespace("oos", instrument="NIFTY")
        self.assertEqual(ns.collection_for("strategy_votes"),
                         "strategy_votes_nifty_historical")

    def test_nifty_streams(self) -> None:
        self.assertEqual(resolve_namespace("live", instrument="NIFTY").stream_for("snapshot"),
                         "market:nifty:snapshot:v1")
        self.assertEqual(resolve_namespace("oos", instrument="NIFTY").stream_for("snapshot"),
                         "market:nifty:snapshot:v1:historical")
        self.assertEqual(
            resolve_namespace("sim", run_id="r1", instrument="NIFTY").stream_for("snapshots"),
            "stream:snapshots:nifty:sim:r1",
        )

    def test_nifty_state_keys(self) -> None:
        self.assertEqual(resolve_namespace("live", instrument="NIFTY").state_key_for("k"),
                         "live:nifty:k")
        self.assertEqual(
            resolve_namespace("sim", run_id="r1", instrument="NIFTY").state_key_for("k"),
            "sim:nifty:r1:k",
        )

    def test_nifty_run_dir_and_lock(self) -> None:
        ns = resolve_namespace("live", instrument="NIFTY")
        self.assertEqual(ns.run_dir_for(), Path("/app/.run/strategy_app_nifty"))
        self.assertEqual(ns.lock_key_for(),
                         "strategy_app_nifty:consumer_lock:market:nifty:snapshot:v1")

    def test_unknown_instrument_is_allowed_as_slug(self) -> None:
        # The namespace layer is registry-agnostic: it slugs any name. Validation
        # against the InstrumentSpec registry happens at the strategy boundary.
        ns = resolve_namespace("live", instrument="FINNIFTY")
        self.assertEqual(ns.collection_for("strategy_positions"),
                         "strategy_positions_finnifty")


class TestInstrumentKindCrossProduct(unittest.TestCase):
    """Every (instrument, kind) pair must yield a UNIQUE collection name —
    this is the structural guarantee that two stacks never collide in Mongo."""

    def test_all_six_namespaces_distinct(self) -> None:
        combos = [
            resolve_namespace("live", instrument="BANKNIFTY"),
            resolve_namespace("oos", instrument="BANKNIFTY"),
            resolve_namespace("sim", run_id="r1", instrument="BANKNIFTY"),
            resolve_namespace("live", instrument="NIFTY"),
            resolve_namespace("oos", instrument="NIFTY"),
            resolve_namespace("sim", run_id="r1", instrument="NIFTY"),
        ]
        names = {ns.collection_for("strategy_positions") for ns in combos}
        self.assertEqual(len(names), 6, f"namespaces collide: {names}")

    def test_live_instruments_distinct_for_all_resources(self) -> None:
        bn = resolve_namespace("live", instrument="BANKNIFTY")
        nf = resolve_namespace("live", instrument="NIFTY")
        self.assertNotEqual(bn.collection_for("strategy_votes"),
                            nf.collection_for("strategy_votes"))
        self.assertNotEqual(bn.stream_for("snapshot"), nf.stream_for("snapshot"))
        self.assertNotEqual(bn.state_key_for("k"), nf.state_key_for("k"))
        self.assertNotEqual(bn.run_dir_for(), nf.run_dir_for())
        self.assertNotEqual(bn.lock_key_for(), nf.lock_key_for())


if __name__ == "__main__":
    unittest.main()
