"""ProtectiveStopManager — broker-side disaster stop unit tests.

Covers the three paths that matter with real money:
  1. entry fill → stop armed at the right trigger with the FILLED qty
  2. app exit → resting stop cancelled BEFORE the market sell proceeds
  3. broker stop fired during an outage → app exit reconciles from the stop's
     fill and does NOT place a second sell (no naked short)
"""

from __future__ import annotations

import json

import pytest

from execution_app.adapter.base import OrderResult
from execution_app.protective_stop import ProtectiveStopManager, _KEY_PREFIX


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, k, v, ex=None):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)


class FakeSignal:
    signal_id = "sig-1"
    direction = "CE"
    strike = 58500
    expiry = __import__("datetime").date(2026, 7, 30)


class FakeAdapter:
    """Dhan-shaped adapter with scriptable stop/cancel/status behaviour."""

    def __init__(self):
        self.placed_stops: list[dict] = []
        self.cancelled: list[str] = []
        self.cancel_ok = True
        self.status_result = OrderResult("stop-1", "pending", None, None, None)
        self.stop_result = OrderResult("stop-1", "placed", None, None, None)

    def place_protective_stop(self, signal, *, qty_units, trigger_price):
        self.placed_stops.append({"qty": qty_units, "trigger": trigger_price})
        return self.stop_result

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return self.cancel_ok

    def get_order_status(self, order_id):
        return self.status_result


@pytest.fixture
def mgr(monkeypatch):
    # Feature is default-OFF after the 2026-07-09 incident; tests exercise it ON.
    monkeypatch.setenv("EXEC_BROKER_STOP_ENABLED", "1")
    adapter = FakeAdapter()
    r = FakeRedis()
    return ProtectiveStopManager(adapter, r), adapter, r


LEG_KEY = _KEY_PREFIX + "leg:CE:58500"


def test_entry_fill_arms_stop_with_filled_qty_and_trigger(mgr, monkeypatch):
    m, adapter, r = mgr
    monkeypatch.setenv("EXEC_BROKER_STOP_PCT", "0.25")
    result = m.protect_entry(position_id="pos-1", signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    assert result is not None and result.status == "placed"
    assert adapter.placed_stops == [{"qty": 30, "trigger": 300.0}]  # 400 * (1 - 0.25)
    # Keyed by leg, NOT position_id — ENTRY signals carry position_id=None live.
    state = json.loads(r.store[LEG_KEY])
    assert state["order_id"] == "stop-1"
    assert state["qty"] == 30


def test_entry_without_position_id_still_arms(mgr):
    m, adapter, r = mgr
    result = m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    assert result is not None and result.status == "placed"
    assert LEG_KEY in r.store


def test_reentry_same_leg_cancels_stale_stop_first(mgr):
    m, adapter, r = mgr
    m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=380.0, fill_qty=30)
    assert adapter.cancelled == ["stop-1"]  # stale stop cancelled before re-arming
    assert len(adapter.placed_stops) == 2


def test_disabled_env_is_noop(mgr, monkeypatch):
    m, adapter, r = mgr
    monkeypatch.setenv("EXEC_BROKER_STOP_ENABLED", "0")
    assert m.protect_entry(position_id="p", signal=FakeSignal(), fill_price=400.0, fill_qty=30) is None
    assert adapter.placed_stops == []


def test_missing_fill_data_does_not_place(mgr):
    m, adapter, r = mgr
    assert m.protect_entry(position_id="p", signal=FakeSignal(), fill_price=None, fill_qty=30) is None
    assert m.protect_entry(position_id="p", signal=FakeSignal(), fill_price=400.0, fill_qty=None) is None
    assert adapter.placed_stops == []


def test_adapter_without_stop_support_is_noop(monkeypatch):
    monkeypatch.setenv("EXEC_BROKER_STOP_ENABLED", "1")

    class Bare:  # paper/kite adapters have no place_protective_stop
        pass

    m = ProtectiveStopManager(Bare(), FakeRedis())
    assert m.protect_entry(position_id="p", signal=FakeSignal(), fill_price=400.0, fill_qty=30) is None


def test_exit_cancels_resting_stop_then_proceeds(mgr):
    m, adapter, r = mgr
    m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    # exit signal carries position_id AND leg fields; leg key must match
    assert m.reconcile_before_exit("pos-1", direction="CE", strike=58500) is None
    assert adapter.cancelled == ["stop-1"]
    assert LEG_KEY not in r.store


def test_exit_falls_back_to_position_id_key(mgr):
    # stops armed before the leg-keying fix (or manually) are pos-keyed
    m, adapter, r = mgr
    r.set(_KEY_PREFIX + "pos-legacy", json.dumps({"order_id": "stop-9", "qty": 30}))
    assert m.reconcile_before_exit("pos-legacy", direction="CE", strike=58500) is None
    assert adapter.cancelled == ["stop-9"]
    assert _KEY_PREFIX + "pos-legacy" not in r.store


def test_exit_with_no_stop_on_record_proceeds(mgr):
    m, adapter, r = mgr
    assert m.reconcile_before_exit("unknown-pos", direction="PE", strike=1) is None
    assert adapter.cancelled == []


def test_broker_stop_already_fired_skips_market_sell(mgr):
    m, adapter, r = mgr
    m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    adapter.cancel_ok = False  # cancel fails because the stop already executed
    adapter.status_result = OrderResult("stop-1", "filled", 299.5, 30, None)
    result = m.reconcile_before_exit("pos-1", direction="CE", strike=58500)
    assert result is not None and result.is_filled
    assert result.fill_price == 299.5  # caller emits the EXIT fill from this
    assert LEG_KEY not in r.store


def test_ambiguous_cancel_retries_then_proceeds(mgr):
    m, adapter, r = mgr
    m.protect_entry(position_id=None, signal=FakeSignal(), fill_price=400.0, fill_qty=30)
    adapter.cancel_ok = False
    adapter.status_result = OrderResult("stop-1", "pending", None, None, None)
    assert m.reconcile_before_exit("pos-1", direction="CE", strike=58500) is None
    assert adapter.cancelled == ["stop-1", "stop-1"]  # retried once
    assert LEG_KEY not in r.store
