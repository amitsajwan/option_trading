"""Unit tests for the seller's refined entry-DTE gate (brain) and DTE-driven exit (manager).

These lock the validated-config logic so the wiring can't silently regress:
  - brain: sit out when DTE < SELLER_MIN_DTE (no theta runway) or IV < floor (thin premium)
  - brain: fire an iron condor when both gates pass
  - manager.check_exit: TP@50% / stop@2x / DTE-exit / max-hold backstop, in the right priority
The regime classifier is stubbed so these isolate the gate/exit logic (the unit under test).
"""
from __future__ import annotations

import pytest

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.seller import brain as brain_mod
from strategy_app.seller.brain import SellerBrain
from strategy_app.seller.executor import FilledLeg, OpenSpread
from strategy_app.seller.manager import PositionManager


def _snap(*, dte, iv, fut=54000.0, step=100, n=12) -> SnapshotAccessor:
    """Minimal snapshot with controllable DTE / IV-rank / chain around `fut`."""
    base = int(round(fut / step) * step)
    strikes = [
        {"strike": base + i * step, "ce_ltp": 120.0, "pe_ltp": 120.0}
        for i in range(-n, n + 1)
    ]
    payload = {
        "session_context": {"days_to_expiry": dte, "date": "2024-08-01", "time": "10:05:00"},
        "futures_bar": {"fut_close": fut},
        "iv_derived": {"iv_percentile": iv},
        "strikes": strikes,
    }
    return SnapshotAccessor(payload)


@pytest.fixture(autouse=True)
def _stub_regime(monkeypatch):
    # Route to the condor default (MID regime) so tests isolate the DTE/IV gates.
    monkeypatch.setattr(brain_mod, "regime_quality", lambda s: ("MID", None))


# ── brain: entry-DTE gate ────────────────────────────────────────────────────
def test_dte_gate_blocks_near_expiry(monkeypatch):
    monkeypatch.setenv("SELLER_MIN_DTE", "4")
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "30")
    d = SellerBrain().decide(_snap(dte=1, iv=60))
    assert not d.fires
    assert "DTE" in d.reason


def test_dte_gate_allows_with_runway(monkeypatch):
    monkeypatch.setenv("SELLER_MIN_DTE", "4")
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "30")
    monkeypatch.setenv("SELLER_CONDOR_OFFSET", "200")
    d = SellerBrain().decide(_snap(dte=5, iv=60))
    assert d.fires
    assert d.structure == "iron_condor"
    assert len(d.legs) == 4


def test_iv_gate_blocks_thin_premium(monkeypatch):
    monkeypatch.setenv("SELLER_MIN_DTE", "4")
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "30")
    d = SellerBrain().decide(_snap(dte=6, iv=10))
    assert not d.fires
    assert "IV" in d.reason


def test_missing_dte_does_not_block(monkeypatch):
    # If the feed omits DTE, don't hard-block (None -> skip the gate, not sit out).
    monkeypatch.setenv("SELLER_MIN_DTE", "4")
    monkeypatch.setenv("SELLER_IV_RANK_MIN", "30")
    monkeypatch.setenv("SELLER_CONDOR_OFFSET", "200")
    d = SellerBrain().decide(_snap(dte=None, iv=60))
    assert d.fires


# ── manager: DTE-driven exit ─────────────────────────────────────────────────
def _spread(credit=100.0):
    legs = [
        FilledLeg("SELL", "PE", 53800, 30, 130.0),
        FilledLeg("BUY", "PE", 53500, 30, 70.0),
        FilledLeg("SELL", "CE", 54200, 30, 130.0),
        FilledLeg("BUY", "CE", 54500, 30, 70.0),
    ]
    return OpenSpread(spread_id="s1", structure="iron_condor", expiry="2024-08-08", qty=30,
                      legs=legs, entry_credit=credit, width=300, opened_at="t", trade_date="2024-08-01")


def test_exit_take_profit_50():
    m = PositionManager.__new__(PositionManager)  # no store/disk
    assert m.check_exit(_spread(100.0), value=49.0, days_held=2, dte=5) == "take_profit_50"


def test_exit_stop_2x():
    m = PositionManager.__new__(PositionManager)
    assert m.check_exit(_spread(100.0), value=210.0, days_held=2, dte=5) == "stop_2x"


def test_exit_dte_fires_before_gamma():
    # DTE_EXIT defaults to 1 (close the day before expiry, out before gamma/STT).
    m = PositionManager.__new__(PositionManager)
    # value between TP and stop, but DTE hit -> dte_exit
    assert m.check_exit(_spread(100.0), value=120.0, days_held=2, dte=1) == "dte_exit"


def test_exit_holds_when_runway_and_midrange():
    m = PositionManager.__new__(PositionManager)
    # not TP, not stop, DTE high, under max-hold -> keep holding (None)
    assert m.check_exit(_spread(100.0), value=120.0, days_held=2, dte=5) is None


# ── max_risk: condor uses ONE wing, not two (review fix) ─────────────────────
def test_condor_max_risk_is_single_wing():
    # OpenSpread.width is stored as 2x wing (600) for a condor; max_risk must be wing(300) - credit.
    sp = _spread(credit=100.0)            # structure="iron_condor"
    sp.width = 600                         # 2 x 300 wing, as the executor stores it
    assert sp.max_risk == 300.0 - 100.0   # 200, NOT 600 - 100 = 500


def test_vertical_max_risk_is_full_width():
    sp = _spread(credit=80.0)
    sp.width = 300
    sp.structure = "bull_put"
    assert sp.max_risk == 300.0 - 80.0    # 220
