"""Regression tests for the 2026-09-25 close-expiry bug.

Root cause: both close call sites in SellerRunner.on_snapshot() passed the
snapshot's rolling front-month expiry (self._expiry(snap), re-derived every
cycle and which rolls to the NEXT week's contract once the current one
lapses) into close_spread() instead of the spread's OWN stored expiry
(sp.expiry). The moment real time crossed a position's actual expiry, every
retry tried to resolve its strikes against a contract from the wrong week --
a combination that can never exist -- so the close failed identically
forever with no possibility of self-recovery. One real NIFTY spread retried
this way every 30s for 10 straight days (30,252 consecutive failures)
before this was found by reading seller_trades.jsonl directly.

The fix (_close_expiry()) mirrors a fix already applied to the DTE-trigger
decision itself (see the 2026-07-10 comment on dte_own a few lines above
the close call sites) -- that earlier fix covered WHEN to close; it never
covered the execution of the close itself, which is what actually failed.
"""
from __future__ import annotations

import datetime

import pytest

from strategy_app.seller.executor import FilledLeg, OpenSpread
from strategy_app.seller.gateway import Fill
from strategy_app.seller.manager import PositionStore
from strategy_app.seller.runner import SellerRunner


class _FakeColl:
    def find_one(self, *a, **k):
        return None

    def update_one(self, *a, **k):
        pass

    def insert_one(self, *a, **k):
        pass

    def delete_one(self, *a, **k):
        pass


class _FakeDb:
    def __getitem__(self, name):
        return _FakeColl()


class _ExpiryCapturingGateway:
    """Records the expiry it was actually called with, per leg."""

    def __init__(self, fill_price: float = 10.0):
        self.calls: list[tuple[str, str, int, datetime.date]] = []
        self._fill_price = fill_price

    def execute(self, action, option_type, strike, expiry, qty) -> Fill:
        self.calls.append((action, option_type, strike, expiry))
        return Fill(True, self._fill_price)


def _stuck_condor(own_expiry: str) -> OpenSpread:
    """A spread whose real contract expired `own_expiry`, deep in the past
    relative to whatever the snapshot's rolling front-month has since become."""
    legs = [
        FilledLeg(action="SELL", option_type="CE", strike=24700, qty=65, entry_price=80.0),
        FilledLeg(action="SELL", option_type="PE", strike=24500, qty=65, entry_price=75.0),
        FilledLeg(action="BUY", option_type="CE", strike=24900, qty=65, entry_price=20.0),
        FilledLeg(action="BUY", option_type="PE", strike=24300, qty=65, entry_price=18.0),
    ]
    return OpenSpread(
        spread_id="stuck1", structure="iron_condor", expiry=own_expiry, qty=65, legs=legs,
        entry_credit=round((80.0 + 75.0) - (20.0 + 18.0), 2), width=400,
        opened_at=f"{own_expiry}T10:00:00+05:30", trade_date="2026-07-06",
    )


def _snap_with_rolled_expiry(day: str, dte_from_snapshot: int) -> dict:
    """A snapshot whose OWN days_to_expiry has rolled forward to the NEXT
    week's contract -- exactly what happens once the front month a stuck
    spread was opened against has already lapsed."""
    strikes = []
    for k in range(24000, 25501, 100):
        strikes.append({"strike": k, "ce_ltp": max(5.0, (24700 - k) * 0.5 + 100),
                        "pe_ltp": max(5.0, (k - 24700) * 0.5 + 100),
                        "ce_oi": 1000, "pe_oi": 1000})
    return {
        "strikes": strikes,
        "trade_date_ist": day,
        "session_context": {"date": day, "time": "10:00:00", "days_to_expiry": dte_from_snapshot},
        "timestamp": f"{day}T10:00:00+05:30",
        "futures_bar": {"fut_close": 24700.0},
    }


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    gw = _ExpiryCapturingGateway()
    r = SellerRunner(_FakeDb(), gateway_factory=lambda pf: gw)
    r._mgr._store = PositionStore(str(tmp_path / "spreads.json"))
    r._gw = gw  # stash for assertions
    return r


def test_close_expiry_uses_spreads_own_expiry_not_snapshot_rolling_one():
    """Direct unit test of the fix: _close_expiry() must return the spread's
    own stored expiry, never the (possibly wrong, rolled-forward) fallback."""
    r = SellerRunner.__new__(SellerRunner)  # bypass __init__, only exercising a pure helper
    import logging
    r.logger = logging.getLogger("test")
    sp = _stuck_condor(own_expiry="2026-07-13")
    rolled_fallback = datetime.date(2026, 7, 28)  # what self._expiry(snap) would now return

    result = r._close_expiry(sp, rolled_fallback)

    assert result == datetime.date(2026, 7, 13), (
        "must resolve to the spread's OWN expiry, not the snapshot's rolled-forward one"
    )
    assert result != rolled_fallback


def test_close_expiry_falls_back_only_when_spreads_own_expiry_is_unparseable(caplog):
    r = SellerRunner.__new__(SellerRunner)
    import logging
    r.logger = logging.getLogger("test")
    sp = _stuck_condor(own_expiry="not-a-date")
    fallback = datetime.date(2026, 7, 28)

    result = r._close_expiry(sp, fallback)

    assert result == fallback


def test_on_snapshot_close_call_passes_spreads_own_expiry_end_to_end(runner):
    """The actual bug, exercised through on_snapshot(): a spread opened
    against 2026-07-13 is still open while the snapshot's own rolling
    days_to_expiry now implies a MUCH later front-month (2026-08-03) --
    exactly the state after the original contract has lapsed. dte_own must
    still evaluate against the spread's real expiry (triggering dte_exit),
    and -- the actual fix -- the gateway must be called with 2026-07-13,
    never the rolled 2026-08-03."""
    sp = _stuck_condor(own_expiry="2026-07-13")
    runner._mgr._open.append(sp)

    # trade_date_ist = 2026-07-13 (the spread's own expiry day) so dte_own <= DTE_EXIT
    # fires dte_exit, while the snapshot's OWN days_to_expiry (21) implies the
    # front-month has already rolled to something in early August -- reproducing
    # the exact mismatch that caused the real incident.
    snap = _snap_with_rolled_expiry(day="2026-07-13", dte_from_snapshot=21)

    runner.on_snapshot(snap)

    assert runner._gw.calls, "close_spread should have attempted to execute leg orders"
    called_expiries = {c[3] for c in runner._gw.calls}
    assert called_expiries == {datetime.date(2026, 7, 13)}, (
        f"close legs must be resolved against the spread's OWN expiry (2026-07-13), "
        f"not the snapshot's rolled-forward one -- got {called_expiries}"
    )
    # And the position should have actually closed (gateway fills every leg), proving
    # the fix doesn't just avoid the wrong expiry -- it lets the close SUCCEED.
    assert not runner._mgr.open_spreads, "spread should be fully closed and removed from tracking"


def test_close_failure_records_error_detail_on_the_spread(runner):
    """2026-09-25: close_failed used to log only a generic reason -- the real
    broker/resolve error was never captured anywhere durable. Now it must be
    recorded on the spread itself so the caller can persist it."""
    class _AlwaysRejectGateway:
        def execute(self, action, option_type, strike, expiry, qty) -> Fill:
            return Fill(False, 0.0, error="dhan resolve: no contract for this expiry/strike")

    runner._gw_factory = lambda pf: _AlwaysRejectGateway()
    sp = _stuck_condor(own_expiry="2026-07-13")
    runner._mgr._open.append(sp)
    snap = _snap_with_rolled_expiry(day="2026-07-13", dte_from_snapshot=21)

    runner.on_snapshot(snap)

    assert sp.last_close_error is not None
    assert "no contract for this expiry/strike" in sp.last_close_error
    assert runner._mgr.open_spreads, "a failed close must keep the spread tracked, not drop it"


def test_close_failure_alerts_once_after_threshold(runner, monkeypatch):
    """2026-09-25: a stuck close used to retry silently forever with no
    alert -- that's how a 10-day, 30,252-attempt failure went unnoticed.
    Now it must alert exactly once when the failure streak crosses the
    configured threshold."""
    class _AlwaysRejectGateway:
        def execute(self, action, option_type, strike, expiry, qty) -> Fill:
            return Fill(False, 0.0, error="dhan resolve: expired contract")

    runner._gw_factory = lambda pf: _AlwaysRejectGateway()
    runner._close_fail_alert_threshold = 3
    alerts: list[str] = []
    monkeypatch.setattr(runner, "_alert", lambda text: alerts.append(text))

    sp = _stuck_condor(own_expiry="2026-07-13")
    runner._mgr._open.append(sp)

    for i in range(5):
        snap = _snap_with_rolled_expiry(day="2026-07-13", dte_from_snapshot=21)
        runner.on_snapshot(snap)

    stuck_alerts = [a for a in alerts if "SELLER CLOSE STUCK" in a]
    assert len(stuck_alerts) == 1, f"expected exactly one escalation alert, got {len(stuck_alerts)}: {stuck_alerts}"
