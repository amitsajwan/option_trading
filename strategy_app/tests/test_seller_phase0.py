"""Phase 0 safety tests (Seller v2 plan, 2026-07-10):
entry-failure latch, margin pre-check, trade card, seller-priority flag,
and buyer-side conflict gate. These are the guards that turn 2026-07-09's
real-money incidents (30s retry burn, margin-blind condor opens, buyer
locking the seller out) into impossible states.
"""
from __future__ import annotations

import datetime

import pytest

from strategy_app.risk import seller_conflict
from strategy_app.seller.brain import SellerDecision, SpreadLeg
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


class _FailingGateway:
    """Every leg fails — the shape of a margin-rejected open."""

    calls = 0

    def execute(self, action, option_type, strike, expiry, qty):
        from strategy_app.seller.gateway import Fill
        _FailingGateway.calls += 1
        return Fill(False, 0.0, error="RMS: insufficient funds")


def _snap(hhmm="11:00", day="2026-07-10"):
    strikes = []
    for k in range(56000, 58501, 100):
        strikes.append({"strike": k, "ce_ltp": max(5.0, (57200 - k) * 0.5 + 200),
                        "pe_ltp": max(5.0, (k - 57200) * 0.5 + 200),
                        "ce_oi": 1000, "pe_oi": 1000})
    return {
        "strikes": strikes,
        "trade_date_ist": day,
        "session_context": {"date": day, "time": f"{hhmm}:00", "days_to_expiry": 18},
        "timestamp": f"{day}T{hhmm}:00+05:30",
        "futures_bar": {"fut_close": 57200.0},
    }


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SELLER_ENTRY_FAIL_LATCH", "2")
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "0")   # let the brain fire in tests
    r = SellerRunner(_FakeDb(), gateway_factory=lambda pf: _FailingGateway())
    r._mgr._store = PositionStore(str(tmp_path / "spreads.json"))
    return r


def test_entry_failure_latches_for_the_day(runner):
    _FailingGateway.calls = 0
    for _ in range(6):  # 6 poll cycles; without the latch each would burn legs
        runner.on_snapshot(_snap())
    assert runner._entry_fail_count >= runner._entry_fail_latch
    calls_at_latch = _FailingGateway.calls
    runner.on_snapshot(_snap())          # further cycles must not touch the gateway
    assert _FailingGateway.calls == calls_at_latch


def test_latch_resets_on_new_day(runner):
    for _ in range(3):
        runner.on_snapshot(_snap(day="2026-07-10"))
    assert runner._entry_fail_count >= 2
    runner.on_snapshot(_snap(day="2026-07-11", hhmm="09:20"))
    assert runner._entry_fail_count == 0


def test_trade_card_contains_max_loss_and_sense(runner):
    legs = [SpreadLeg("SELL", "CE", 57400, "short"), SpreadLeg("BUY", "CE", 57700, "long"),
            SpreadLeg("SELL", "PE", 57000, "short"), SpreadLeg("BUY", "PE", 56700, "long")]
    decision = SellerDecision("iron_condor", legs=legs, regime="RANGE", iv_rank=45.0)
    pf = lambda ot, s: 100.0
    card = runner._trade_card(decision, pf, datetime.date(2026, 7, 28), 250000.0)
    assert "max loss" in card and "max profit" in card
    assert "NEUTRAL" in card
    assert "free margin" in card


def test_seller_flag_published_and_buyer_gate_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    seller_conflict.publish_seller_state(1)
    assert seller_conflict.seller_spread_active() is True
    seller_conflict.publish_seller_state(0)
    assert seller_conflict.seller_spread_active() is False


def test_stale_flag_is_ignored(tmp_path, monkeypatch):
    import os, time
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    seller_conflict.publish_seller_state(1)
    old = time.time() - 3600
    os.utime(seller_conflict.flag_path(), (old, old))
    assert seller_conflict.seller_spread_active() is False


def test_thin_credit_skipped_before_any_leg(tmp_path, monkeypatch):
    """Replay finding: negative/thin-credit structures must be rejected on the
    ESTIMATE, before the gateway is ever touched (live unwinds cost real money)."""
    monkeypatch.setenv("STRATEGY_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "0")
    monkeypatch.setenv("SELLER_MIN_CREDIT_FRAC", "0.99")  # impossible floor
    _FailingGateway.calls = 0
    r = SellerRunner(_FakeDb(), gateway_factory=lambda pf: _FailingGateway())
    r._mgr._store = PositionStore(str(tmp_path / "spreads.json"))
    for _ in range(3):
        r.on_snapshot(_snap())
    assert _FailingGateway.calls == 0        # gateway never touched
    assert r._entry_fail_count == 0          # a skip is not a failure


def test_unvaluable_spread_still_exits_on_max_hold(tmp_path, monkeypatch):
    """2026-07-10 root-cause: strikes drifted off the chain -> spread_value None
    -> ALL exit checks skipped -> spread frozen 11 months in replay (and would
    strand live multi-day holds). Time-based exits must fire without a price."""
    monkeypatch.setenv("STRATEGY_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    from strategy_app.seller.executor import FilledLeg, OpenSpread
    from strategy_app.seller.gateway import Fill

    class _AlwaysFillGateway:
        def execute(self, action, option_type, strike, expiry, qty):
            return Fill(True, 10.0, order_id="x")

    r = SellerRunner(_FakeDb(), gateway_factory=lambda pf: _AlwaysFillGateway())
    r._mgr._store = PositionStore(str(tmp_path / "spreads.json"))
    stale = OpenSpread(
        spread_id="stuck1", structure="iron_condor", expiry="2026-08-25", qty=30,
        legs=[FilledLeg("SELL", "CE", 99000, 30, 100.0),   # strikes far off any chain
              FilledLeg("BUY", "CE", 99300, 30, 50.0)],
        entry_credit=50.0, width=600, opened_at="2026-06-20T05:00:00+00:00",
        trade_date="2026-06-20",
    )
    r._mgr.add(stale)
    r.on_snapshot(_snap(day="2026-07-10"))   # held 20d >> MAX_HOLD
    assert all(s.spread_id != "stuck1" for s in r._mgr.open_spreads), \
        "unvaluable spread must exit via max_hold, not freeze"


def test_priority_gate_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("SELLER_PRIORITY_ENABLED", "0")
    seller_conflict.publish_seller_state(2)
    assert seller_conflict.seller_spread_active() is False
