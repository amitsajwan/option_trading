"""Tests for RiskGates.has_conflicting_bn_position — the cross-system guard added
2026-07-08 when the seller went live on real money alongside the BN buy-side, sharing
one Dhan account with no coordination otherwise."""
from __future__ import annotations

from strategy_app.seller.manager import RiskGates


class _FakeColl:
    def __init__(self, doc):
        self._doc = doc

    def find_one(self, *args, **kwargs):
        return self._doc


class _FakeDb:
    def __init__(self, doc):
        self._coll = _FakeColl(doc)

    def __getitem__(self, name):
        assert name == "strategy_positions"
        return self._coll


def test_blocks_when_bn_position_open():
    db = _FakeDb({"event": "POSITION_OPEN", "direction": "CE", "strike": 59100})
    ok, why = RiskGates().has_conflicting_bn_position(db)
    assert ok is False
    assert "59100" in why


def test_blocks_when_bn_position_managing():
    db = _FakeDb({"event": "POSITION_MANAGE", "direction": "PE", "strike": 58000})
    ok, why = RiskGates().has_conflicting_bn_position(db)
    assert ok is False


def test_allows_when_latest_bn_event_is_close():
    db = _FakeDb({"event": "POSITION_CLOSE", "direction": "CE", "strike": 59100})
    ok, why = RiskGates().has_conflicting_bn_position(db)
    assert ok is True


def test_allows_when_no_bn_positions_ever():
    db = _FakeDb(None)
    ok, why = RiskGates().has_conflicting_bn_position(db)
    assert ok is True


def test_fails_closed_on_query_error():
    class _BrokenColl:
        def find_one(self, *a, **k):
            raise RuntimeError("mongo down")

    class _BrokenDb:
        def __getitem__(self, name):
            return _BrokenColl()

    ok, why = RiskGates().has_conflicting_bn_position(_BrokenDb())
    assert ok is False
    assert "fail-closed" in why
