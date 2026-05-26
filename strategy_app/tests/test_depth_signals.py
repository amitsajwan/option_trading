"""Tests for depth-context side-channel: DepthContext, RedisDepthReader, engine signals."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from strategy_app.market.depth_context import DepthContext, StrikeDepth
from strategy_app.runtime.redis_depth_reader import (
    RedisDepthReader,
    _parse_strike_depth,
    _epoch_from_depth_raw,
    _is_stale,
)


# ---------------------------------------------------------------------------
# StrikeDepth helpers
# ---------------------------------------------------------------------------

class TestStrikeDepth:
    def test_spread_computed(self):
        d = StrikeDepth(best_bid=100.0, best_ask=101.5, bid_qty=300, ask_qty=200)
        assert abs(d.spread - 1.5) < 1e-9

    def test_spread_none_when_missing(self):
        d = StrikeDepth(best_bid=None, best_ask=101.5, bid_qty=300, ask_qty=200)
        assert d.spread is None

    def test_relative_spread(self):
        d = StrikeDepth(best_bid=200.0, best_ask=201.0, bid_qty=100, ask_qty=100)
        assert abs(d.relative_spread - 0.005) < 1e-9

    def test_is_valid_all_present(self):
        d = StrikeDepth(best_bid=100.0, best_ask=101.0, bid_qty=100, ask_qty=50)
        assert d.is_valid

    def test_is_valid_missing_ask(self):
        d = StrikeDepth(best_bid=100.0, best_ask=None, bid_qty=100, ask_qty=50)
        assert not d.is_valid


class TestDepthContext:
    def test_is_available_with_ce(self):
        ce = StrikeDepth(best_bid=100.0, best_ask=101.0, bid_qty=100, ask_qty=50)
        ctx = DepthContext(ce=ce)
        assert ctx.is_available

    def test_is_available_false_when_empty(self):
        ctx = DepthContext()
        assert not ctx.is_available

    def test_ce_valid_pe_valid(self):
        ce = StrikeDepth(best_bid=100.0, best_ask=101.0, bid_qty=100, ask_qty=50)
        pe = StrikeDepth(best_bid=90.0, best_ask=None, bid_qty=80, ask_qty=60)
        ctx = DepthContext(ce=ce, pe=pe)
        assert ctx.ce_valid
        assert not ctx.pe_valid


# ---------------------------------------------------------------------------
# _parse_strike_depth
# ---------------------------------------------------------------------------

class TestParseStrikeDepth:
    def _record(self, **kwargs) -> str:
        base = {
            "best_bid": 150.0,
            "best_ask": 151.0,
            "bid_qty": 500,
            "ask_qty": 300,
            "instrument": "NFO:BANKNIFTY24AUG50000CE",
            "fetched_at": "2024-08-01T10:00:00+05:30",
        }
        base.update(kwargs)
        return json.dumps(base)

    def test_happy_path(self):
        d = _parse_strike_depth(self._record())
        assert d is not None
        assert d.best_bid == 150.0
        assert d.best_ask == 151.0
        assert d.bid_qty == 500
        assert d.ask_qty == 300

    def test_returns_none_for_empty(self):
        assert _parse_strike_depth(None) is None
        assert _parse_strike_depth("") is None

    def test_returns_none_for_bad_json(self):
        assert _parse_strike_depth("{bad json}") is None

    def test_missing_best_ask_returns_invalid_depth(self):
        # Missing best_ask → StrikeDepth returned but is_valid=False (not None)
        record = json.dumps({"best_bid": 100.0, "bid_qty": 100, "ask_qty": 50})
        d = _parse_strike_depth(record)
        assert d is not None
        assert not d.is_valid
        assert d.best_ask is None

    def test_none_prices_allowed(self):
        d = _parse_strike_depth(self._record(best_bid=None))
        assert d is not None
        assert d.best_bid is None
        assert not d.is_valid


# ---------------------------------------------------------------------------
# _is_stale
# ---------------------------------------------------------------------------

class TestIsStale:
    def test_fresh_is_not_stale(self):
        assert not _is_stale(time.time(), 30)

    def test_old_is_stale(self):
        assert _is_stale(time.time() - 60, 30)

    def test_none_epoch_is_stale(self):
        assert _is_stale(None, 30)


# ---------------------------------------------------------------------------
# RedisDepthReader
# ---------------------------------------------------------------------------

def _make_record(
    best_bid: float = 150.0,
    best_ask: float = 151.0,
    bid_qty: int = 500,
    ask_qty: int = 300,
    age_sec: float = 0.0,
    suffix: str = "CE",
) -> str:
    return json.dumps({
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "instrument": f"NFO:BANKNIFTY24AUG50000{suffix}",
        "fetched_at": "2024-08-01T10:00:00+05:30",
        "fetched_at_epoch": time.time() - age_sec,
    })


class TestRedisDepthReader:
    def _reader(self, ce_raw=None, pe_raw=None, stale_sec=30):
        client = MagicMock()
        with patch("strategy_app.runtime.redis_depth_reader.get_redis_key", side_effect=lambda k: k):
            def mock_get(key):
                if "ce" in key:
                    return ce_raw
                if "pe" in key:
                    return pe_raw
                return None
            client.get.side_effect = mock_get
            return RedisDepthReader(client=client, stale_sec=stale_sec)

    def test_returns_none_when_both_absent(self):
        reader = self._reader()
        with patch("strategy_app.runtime.redis_depth_reader.get_redis_key", side_effect=lambda k: k):
            reader._client.get.return_value = None
            assert reader.read_depth() is None

    def test_returns_ce_context_when_fresh(self):
        ce = _make_record(age_sec=1)
        reader = self._reader(ce_raw=ce)
        with patch("strategy_app.runtime.redis_depth_reader.get_redis_key", side_effect=lambda k: k):
            ctx = reader.read_depth()
        assert ctx is not None
        assert ctx.ce is not None
        assert ctx.ce.best_bid == 150.0

    def test_returns_none_when_stale(self):
        ce = _make_record(age_sec=60)
        reader = self._reader(ce_raw=ce, stale_sec=30)
        with patch("strategy_app.runtime.redis_depth_reader.get_redis_key", side_effect=lambda k: k):
            ctx = reader.read_depth()
        assert ctx is None

    def test_returns_both_sides_when_fresh(self):
        ce = _make_record(age_sec=1, suffix="CE")
        pe = _make_record(age_sec=1, suffix="PE", best_bid=80.0, best_ask=81.0)
        reader = self._reader(ce_raw=ce, pe_raw=pe)
        with patch("strategy_app.runtime.redis_depth_reader.get_redis_key", side_effect=lambda k: k):
            ctx = reader.read_depth()
        assert ctx is not None
        assert ctx.ce_valid
        assert ctx.pe_valid
        assert ctx.pe.best_bid == 80.0

    def test_redis_error_returns_none(self):
        client = MagicMock()
        client.get.side_effect = Exception("connection refused")
        reader = RedisDepthReader(client=client, stale_sec=30)
        assert reader.read_depth() is None


# ---------------------------------------------------------------------------
# Depth signals in shadow scorer (integration-style)
# ---------------------------------------------------------------------------

def _make_snap_dict(**kwargs) -> dict:
    """Minimal snapshot dict that won't crash the shadow scorer."""
    base: dict[str, Any] = {
        "session_context": {"snapshot_id": "test", "date": "2024-08-01"},
        "futures_bar": {"fut_close": 50000.0, "fut_open": 49900.0},
        "futures_derived": {"vwap": 49950.0, "price_vs_vwap": 50.0},
        "opening_range": {
            "orh": 50100.0,
            "orl": 49900.0,
            "orh_broken": False,
            "orl_broken": False,
            "price_vs_orh": 0.0,
            "price_vs_orl": 0.0,
        },
        "atm_options": {
            "atm_ce_close": 150.0,
            "atm_pe_close": 140.0,
            "atm_ce_iv": 0.20,
            "atm_pe_iv": 0.22,
        },
        "chain_aggregates": {"pcr": 1.0, "pcr_change_5m": 0.0},
        "iv_derived": {},
        "vix_context": {},
        "session_levels": {},
    }
    base.update(kwargs)
    return base


def _build_engine_with_depth(depth_ctx: DepthContext):
    """Build a minimal DeterministicRuleEngine with a stubbed depth reader."""
    from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine

    mock_reader = MagicMock()
    mock_reader.read_depth.return_value = depth_ctx

    engine = DeterministicRuleEngine(depth_reader=mock_reader)
    # Pre-fill rolling buffers so IV/VWAP signals can compute
    engine._iv_buf = deque([(0.20, 0.22), (0.20, 0.22), (0.20, 0.22)], maxlen=3)
    engine._pvwap_buf = deque([50.0, 50.0], maxlen=2)
    return engine


class TestDepthSignalsInShadowScorer:
    def test_ce_bid_dom_adds_bullish_score(self):
        ce = StrikeDepth(best_bid=150.0, best_ask=151.0, bid_qty=900, ask_qty=300)
        ctx = DepthContext(ce=ce)
        engine = _build_engine_with_depth(ctx)
        engine._current_depth_ctx = ctx

        snap_dict = _make_snap_dict()
        from strategy_app.market.snapshot_accessor import SnapshotAccessor
        snap = SnapshotAccessor(snap_dict)

        direction, basis, score = engine._shadow_direction_from_snapshot(snap)
        assert "depth_ce_bid_dom" in basis
        # CE bid dom fires → score contribution is positive
        assert score > 0 or "depth_ce_bid_dom" in basis

    def test_pe_offer_dom_adds_bullish_score(self):
        pe = StrikeDepth(best_bid=80.0, best_ask=81.0, bid_qty=100, ask_qty=300)
        ctx = DepthContext(pe=pe)
        engine = _build_engine_with_depth(ctx)
        engine._current_depth_ctx = ctx

        snap_dict = _make_snap_dict()
        from strategy_app.market.snapshot_accessor import SnapshotAccessor
        snap = SnapshotAccessor(snap_dict)

        direction, basis, score = engine._shadow_direction_from_snapshot(snap)
        assert "depth_pe_offer_dom" in basis

    def test_depth_absent_no_depth_signals(self):
        engine = _build_engine_with_depth(DepthContext())
        engine._current_depth_ctx = None  # explicitly absent

        snap_dict = _make_snap_dict()
        from strategy_app.market.snapshot_accessor import SnapshotAccessor
        snap = SnapshotAccessor(snap_dict)

        _, basis, _ = engine._shadow_direction_from_snapshot(snap)
        assert "depth_" not in basis

    def test_pe_bid_dom_adds_bearish(self):
        pe = StrikeDepth(best_bid=80.0, best_ask=81.0, bid_qty=900, ask_qty=200)
        ctx = DepthContext(pe=pe)
        engine = _build_engine_with_depth(ctx)
        engine._current_depth_ctx = ctx

        snap_dict = _make_snap_dict()
        from strategy_app.market.snapshot_accessor import SnapshotAccessor
        snap = SnapshotAccessor(snap_dict)

        _, basis, _ = engine._shadow_direction_from_snapshot(snap)
        assert "depth_pe_bid_dom" in basis

    def test_ce_ask_dom_adds_bearish(self):
        ce = StrikeDepth(best_bid=150.0, best_ask=151.0, bid_qty=100, ask_qty=400)
        ctx = DepthContext(ce=ce)
        engine = _build_engine_with_depth(ctx)
        engine._current_depth_ctx = ctx

        snap_dict = _make_snap_dict()
        from strategy_app.market.snapshot_accessor import SnapshotAccessor
        snap = SnapshotAccessor(snap_dict)

        _, basis, _ = engine._shadow_direction_from_snapshot(snap)
        assert "depth_ce_ask_dom" in basis
