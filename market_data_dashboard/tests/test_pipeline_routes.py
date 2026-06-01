"""Tests for market_data_dashboard/routes/pipeline_routes.py.

Covers all 6 REST endpoints and the helper functions.
Uses a fake in-memory MongoDB collection — no real DB required.
"""
import asyncio
import os
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from unittest import mock

import market_data_dashboard.routes.pipeline_routes as pr
from market_data_dashboard.routes.pipeline_routes import (
    PipelineRouter,
    _collapse_trace,
    _dt_to_str,
    _stage_sort_key,
)


# ---------------------------------------------------------------------------
# Fake MongoDB helpers
# ---------------------------------------------------------------------------

def _dt(offset_sec: float = 0.0) -> datetime:
    return datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_sec)


class _FakeCursor:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def sort(self, spec, *args):
        if isinstance(spec, list):
            for key, direction in reversed(spec):
                self._rows.sort(key=lambda r: (r.get(key) or ""), reverse=(direction == -1))
        return self

    def limit(self, n: int):
        return _FakeCursor(self._rows[:n])

    def __iter__(self):
        return iter(self._rows)


class _FakeCollection:
    def __init__(self, rows: list | None = None) -> None:
        self._rows: list = list(rows or [])

    def find(self, query=None, projection=None, sort=None, limit=None):
        filtered = [deepcopy(r) for r in self._rows if self._match(r, query or {})]
        cursor = _FakeCursor(filtered)
        if sort:
            cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(limit)
        return cursor

    def find_one(self, query=None, projection=None, sort=None):
        results = list(self.find(query=query, sort=sort, limit=1))
        return results[0] if results else None

    def _match(self, doc: dict, query: dict) -> bool:
        for key, val in query.items():
            if key == "_received_at" and isinstance(val, dict):
                doc_val = doc.get("_received_at")
                if "$gt" in val and not (doc_val and doc_val > val["$gt"]):
                    return False
            elif doc.get(key) != val:
                return False
        return True


def _make_doc(stage: str, trace_id: str = "abc123", run_id: str = "run1",
              offset: float = 0.0, **extra) -> dict:
    outcomes = {
        "regime":    "TRENDING 0.88",
        "entry":     "allowed",
        "direction": "CE",
        "depth":     "aligned 0.83",
        "strike":    "49500",
        "risk":      "approved 2L",
        "execution": "ENTER",
    }
    return {
        "trace_id":    trace_id,
        "run_id":      run_id,
        "stage":       stage,
        "outcome":     outcomes.get(stage, "ok"),
        "confidence":  0.80,
        "plugin_id":   f"{stage}_plugin_v1",
        "plugin_version": "1.0",
        "parity_mode": "live_full",
        "timestamp":   _dt(offset).isoformat(),
        "_received_at": _dt(offset),
        "payload":     {"regime": "TRENDING", "confidence": 0.88,
                        "ce_bid_strength": 0.71, "pe_bid_strength": 0.29,
                        "depth_aligned": True, "depth_available": True,
                        "proceed": True, "direction": "CE",
                        "allowed": True},
        **extra,
    }


def _full_trace(trace_id: str = "abc123", run_id: str = "run1", offset: float = 0.0) -> list:
    return [_make_doc(s, trace_id=trace_id, run_id=run_id, offset=offset + i * 0.1)
            for i, s in enumerate(pr._STAGE_ORDER)]


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class StageSortKeyTests(unittest.TestCase):
    def test_known_stages_in_order(self) -> None:
        keys = [_stage_sort_key(s) for s in pr._STAGE_ORDER]
        self.assertEqual(keys, sorted(keys))

    def test_unknown_stage_is_large(self) -> None:
        self.assertGreater(_stage_sort_key("unknown"), _stage_sort_key("execution"))

    def test_regime_is_first(self) -> None:
        self.assertEqual(_stage_sort_key("regime"), 0)


class CollapseTraceTests(unittest.TestCase):
    def test_collapses_all_7_stages(self) -> None:
        docs = _full_trace()
        result = _collapse_trace(docs)
        self.assertEqual(set(result["stages"].keys()), set(pr._STAGE_ORDER))

    def test_regime_shortcuts_extracted(self) -> None:
        docs = _full_trace()
        result = _collapse_trace(docs)
        self.assertEqual(result["regime"], "TRENDING")
        self.assertIn("regime_color", result)

    def test_execution_signal_type(self) -> None:
        docs = _full_trace()
        result = _collapse_trace(docs)
        self.assertEqual(result["signal_type"], "ENTER")

    def test_empty_docs_returns_empty_trace(self) -> None:
        result = _collapse_trace([])
        self.assertEqual(result["trace_id"], "")
        self.assertEqual(result["stages"], {})

    def test_partial_trace_collapses(self) -> None:
        docs = [_make_doc("regime"), _make_doc("entry")]
        result = _collapse_trace(docs)
        self.assertIn("regime", result["stages"])
        self.assertIn("entry",  result["stages"])
        self.assertNotIn("execution", result["stages"])


class DtToStrTests(unittest.TestCase):
    def test_datetime_to_iso(self) -> None:
        dt = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)
        self.assertIn("2026-05-31", _dt_to_str(dt))

    def test_none_returns_empty(self) -> None:
        self.assertEqual(_dt_to_str(None), "")

    def test_string_passthrough(self) -> None:
        self.assertEqual(_dt_to_str("foo"), "foo")


# ---------------------------------------------------------------------------
# Router endpoint tests
# ---------------------------------------------------------------------------

class _RouterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        pr._db_cache.clear()
        self.router = PipelineRouter()
        self._fake_coll = _FakeCollection()
        pr._db_cache["coll"] = self._fake_coll

    def tearDown(self) -> None:
        pr._db_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)


class GetLatestTests(_RouterTestBase):
    def test_empty_collection_returns_empty_list(self) -> None:
        result = self._run(self.router.get_latest(limit=50))
        self.assertEqual(result["traces"], [])
        self.assertEqual(result["total"], 0)

    def test_single_full_trace_returned(self) -> None:
        for doc in _full_trace("t1"):
            self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_latest(limit=50))
        self.assertEqual(len(result["traces"]), 1)
        self.assertEqual(result["traces"][0]["trace_id"], "t1")

    def test_two_traces_grouped_correctly(self) -> None:
        for doc in _full_trace("t1", offset=0.0):
            self._fake_coll._rows.append(doc)
        for doc in _full_trace("t2", offset=100.0):
            self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_latest(limit=50))
        ids = {t["trace_id"] for t in result["traces"]}
        self.assertIn("t1", ids)
        self.assertIn("t2", ids)

    def test_limit_respected(self) -> None:
        for i in range(5):
            for doc in _full_trace(f"trace{i}", offset=float(i * 10)):
                self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_latest(limit=3))
        self.assertLessEqual(len(result["traces"]), 3)

    def test_docs_with_empty_trace_id_skipped(self) -> None:
        doc = _make_doc("regime", trace_id="")
        self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_latest(limit=50))
        self.assertEqual(result["traces"], [])

    def test_mongo_unavailable_returns_error(self) -> None:
        pr._db_cache.clear()  # force None
        with mock.patch.object(pr, "_get_collection", return_value=None):
            result = self._run(self.router.get_latest(limit=50))
        self.assertIn("error", result)
        self.assertEqual(result["traces"], [])


class GetTraceTests(_RouterTestBase):
    def test_returns_404_when_not_found(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._run(self.router.get_trace("missing-trace"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_returns_stages_sorted_by_order(self) -> None:
        for doc in _full_trace("tid1"):
            self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_trace("tid1"))
        self.assertEqual(result["trace_id"], "tid1")
        self.assertEqual(result["stage_count"], 7)
        stage_names = [s["stage"] for s in result["stages"]]
        self.assertEqual(stage_names, sorted(stage_names, key=_stage_sort_key))

    def test_partial_trace_returns_available_stages(self) -> None:
        self._fake_coll._rows.append(_make_doc("regime", trace_id="partial"))
        self._fake_coll._rows.append(_make_doc("entry",  trace_id="partial"))
        result = self._run(self.router.get_trace("partial"))
        self.assertEqual(result["stage_count"], 2)

    def test_mongo_unavailable_returns_error_dict(self) -> None:
        with mock.patch.object(pr, "_get_collection", return_value=None):
            result = self._run(self.router.get_trace("t1"))
        self.assertIn("error", result)


class GetRegimeTimelineTests(_RouterTestBase):
    def test_empty_returns_empty_list(self) -> None:
        result = self._run(self.router.get_regime_timeline(run_id="", limit=200))
        self.assertEqual(result["regimes"], [])

    def test_returns_only_regime_stage_docs(self) -> None:
        for doc in _full_trace("t1"):
            self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_regime_timeline(run_id="", limit=200))
        self.assertEqual(len(result["regimes"]), 1)

    def test_filters_by_run_id(self) -> None:
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t1", run_id="run_a"))
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t2", run_id="run_b"))
        result = self._run(self.router.get_regime_timeline(run_id="run_a", limit=200))
        self.assertEqual(len(result["regimes"]), 1)
        self.assertEqual(result["regimes"][0]["run_id"], "run_a")

    def test_regime_color_included(self) -> None:
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t1"))
        result = self._run(self.router.get_regime_timeline(run_id="", limit=200))
        self.assertIn("color", result["regimes"][0])

    def test_regime_name_extracted_from_payload(self) -> None:
        doc = _make_doc("regime", trace_id="t1")
        doc["payload"]["regime"] = "BREAKOUT"
        self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_regime_timeline(run_id="", limit=200))
        self.assertEqual(result["regimes"][0]["regime"], "BREAKOUT")

    def test_mongo_unavailable_returns_error(self) -> None:
        with mock.patch.object(pr, "_get_collection", return_value=None):
            result = self._run(self.router.get_regime_timeline(run_id="", limit=200))
        self.assertIn("error", result)


class GetDepthCurrentTests(_RouterTestBase):
    def test_no_depth_events_returns_unavailable(self) -> None:
        result = self._run(self.router.get_depth_current(run_id=""))
        self.assertFalse(result.get("depth_available", True))

    def test_returns_latest_depth_event(self) -> None:
        doc = _make_doc("depth", trace_id="t1")
        doc["payload"]["ce_bid_strength"] = 0.72
        doc["payload"]["depth_available"] = True
        self._fake_coll._rows.append(doc)
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t1"))
        result = self._run(self.router.get_depth_current(run_id=""))
        self.assertAlmostEqual(result["ce_bid_strength"], 0.72)

    def test_filters_by_run_id(self) -> None:
        doc_a = _make_doc("depth", trace_id="t1", run_id="run_a")
        doc_b = _make_doc("depth", trace_id="t2", run_id="run_b")
        doc_b["payload"]["ce_bid_strength"] = 0.99
        self._fake_coll._rows.extend([doc_a, doc_b])
        result = self._run(self.router.get_depth_current(run_id="run_b"))
        self.assertAlmostEqual(result["ce_bid_strength"], 0.99)

    def test_depth_aligned_bool_coerced(self) -> None:
        doc = _make_doc("depth", trace_id="t1")
        doc["payload"]["depth_aligned"] = 1  # truthy int, not bool
        self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_depth_current(run_id=""))
        self.assertIs(result["depth_aligned"], True)

    def test_mongo_unavailable_returns_error(self) -> None:
        with mock.patch.object(pr, "_get_collection", return_value=None):
            result = self._run(self.router.get_depth_current(run_id=""))
        self.assertIn("error", result)


class GetPluginsRegistryTests(_RouterTestBase):
    def test_empty_returns_empty_list(self) -> None:
        result = self._run(self.router.get_plugins_registry(run_id=""))
        self.assertEqual(result["plugins"], [])

    def test_deduplicates_same_plugin(self) -> None:
        for i in range(5):
            self._fake_coll._rows.append(_make_doc("regime", trace_id=f"t{i}"))
        result = self._run(self.router.get_plugins_registry(run_id=""))
        regime_plugins = [p for p in result["plugins"] if p["stage"] == "regime"]
        self.assertEqual(len(regime_plugins), 1)

    def test_different_versions_kept_separate(self) -> None:
        doc_v1 = _make_doc("regime", trace_id="t1")
        doc_v1["plugin_version"] = "1.0"
        doc_v2 = _make_doc("regime", trace_id="t2")
        doc_v2["plugin_version"] = "2.0"
        self._fake_coll._rows.extend([doc_v1, doc_v2])
        result = self._run(self.router.get_plugins_registry(run_id=""))
        versions = {p["plugin_version"] for p in result["plugins"] if p["stage"] == "regime"}
        self.assertEqual(versions, {"1.0", "2.0"})

    def test_sorted_by_stage_order(self) -> None:
        for s in pr._STAGE_ORDER:
            self._fake_coll._rows.append(_make_doc(s, trace_id="t1"))
        result = self._run(self.router.get_plugins_registry(run_id=""))
        stages = [p["stage"] for p in result["plugins"]]
        self.assertEqual(stages, sorted(stages, key=_stage_sort_key))

    def test_docs_without_plugin_id_skipped(self) -> None:
        doc = _make_doc("regime")
        doc["plugin_id"] = ""
        self._fake_coll._rows.append(doc)
        result = self._run(self.router.get_plugins_registry(run_id=""))
        self.assertEqual(result["plugins"], [])

    def test_filters_by_run_id(self) -> None:
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t1", run_id="run_x"))
        self._fake_coll._rows.append(_make_doc("regime", trace_id="t2", run_id="run_y"))
        result = self._run(self.router.get_plugins_registry(run_id="run_x"))
        self.assertTrue(all(p["run_id"] == "run_x" for p in result["plugins"]))

    def test_mongo_unavailable_returns_error(self) -> None:
        with mock.patch.object(pr, "_get_collection", return_value=None):
            result = self._run(self.router.get_plugins_registry(run_id=""))
        self.assertIn("error", result)


class GetStreamsHealthTests(_RouterTestBase):
    def test_returns_note_when_no_run_id(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIM_RUN_ID", None)
            with mock.patch.object(pr, "_redis_client", return_value=object()):
                result = self._run(self.router.get_streams_health())
        self.assertIn("note", result)
        self.assertEqual(result["streams"], [])

    def test_returns_error_when_redis_unavailable(self) -> None:
        with mock.patch.object(pr, "_redis_client", return_value=None):
            result = self._run(self.router.get_streams_health())
        self.assertIn("error", result)

    def test_stream_status_computed_correctly(self) -> None:
        """Unit test for the status logic — stale > 30s, warn > 50 lag, else ok."""
        from market_data_dashboard.routes.pipeline_routes import _STAGE_ORDER

        class _FakeRedis:
            def __init__(self, age_sec, lag):
                self._age_sec = age_sec
                self._lag = lag

            def xinfo_groups(self, stream):
                return [{"lag": self._lag, "pending": 0}]

            def xrevrange(self, stream, count):
                import time
                ms = int((time.time() - self._age_sec) * 1000)
                return [(f"{ms}-0", {})]

        for age, lag, expected in [
            (5,  0,  "ok"),
            (5,  60, "warn"),
            (60, 0,  "stale"),
        ]:
            with mock.patch.dict(os.environ, {"SIM_RUN_ID": "test-run"}, clear=False):
                with mock.patch.object(pr, "_redis_client", return_value=_FakeRedis(age, lag)):
                    with mock.patch("contracts_app.resolve_namespace") as mock_ns:
                        ns = mock.MagicMock()
                        ns.stream_for = lambda slug: f"stream:{slug}:sim:test-run"
                        mock_ns.return_value = ns
                        result = self._run(self.router.get_streams_health())
            for s in result.get("streams", []):
                self.assertEqual(s["status"], expected,
                    f"age={age} lag={lag}: expected {expected}, got {s['status']}")


if __name__ == "__main__":
    unittest.main()
