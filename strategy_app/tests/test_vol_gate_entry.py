"""Tests for the non-ML volatility-gate entry strategy."""
from __future__ import annotations

from unittest.mock import MagicMock

from strategy_app.contracts import Direction, SignalType
from strategy_app.engines.strategies.vol_gate_entry import VolGateEntryStrategy


def _snap(atr: float | None = 60.0, fut: float | None = 56000.0, bb: float | None = 0.01) -> dict:
    mtf: dict = {}
    if atr is not None:
        mtf["atr_14_1m"] = atr
    if bb is not None:
        mtf["bb_width_5m"] = bb
    return {
        "snapshot_id": "snap-1",
        "timestamp": "2026-06-12T08:00:00+00:00",
        "trade_date": "2026-06-12",
        "atm_strike": 56000,
        "atm_ce_close": 800.0,
        "atm_pe_close": 790.0,
        "fut_return_5m": 0.002,
        "futures_bar": {"fut_close": fut},
        "mtf_derived": mtf,
    }


def test_fires_when_atr_pct_above_threshold(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")          # deterministic direction
    monkeypatch.setenv("ATR_ENTRY_MIN_PCT", "0.00088")
    monkeypatch.delenv("ATR_ENTRY_MIN_ABS", raising=False)
    monkeypatch.delenv("ATR_ENTRY_BB_MIN", raising=False)
    # atr_pct = 60/56000 = 0.00107 > 0.00088 -> fire
    vote = VolGateEntryStrategy().evaluate(_snap(atr=60.0), None, MagicMock())
    assert vote is not None
    assert vote.strategy_name == "VOL_GATE_ENTRY"
    assert vote.signal_type == SignalType.ENTRY
    assert vote.direction == Direction.CE
    assert vote.raw_signals["trigger"] == "vol_gate"
    assert 0.5 <= vote.confidence <= 1.0


def test_no_fire_when_atr_pct_below_threshold(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    monkeypatch.setenv("ATR_ENTRY_MIN_PCT", "0.00088")
    monkeypatch.delenv("ATR_ENTRY_MIN_ABS", raising=False)
    # atr_pct = 40/56000 = 0.000714 < 0.00088 -> no fire
    vote = VolGateEntryStrategy().evaluate(_snap(atr=40.0), None, MagicMock())
    assert vote is None


def test_absolute_mode_overrides_pct(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    monkeypatch.setenv("ATR_ENTRY_MIN_ABS", "50")        # absolute atr gate
    vote_hi = VolGateEntryStrategy().evaluate(_snap(atr=55.0), None, MagicMock())
    vote_lo = VolGateEntryStrategy().evaluate(_snap(atr=45.0), None, MagicMock())
    assert vote_hi is not None
    assert vote_lo is None


def test_bb_confirm_blocks_when_low(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    monkeypatch.setenv("ATR_ENTRY_MIN_PCT", "0.00088")
    monkeypatch.setenv("ATR_ENTRY_BB_MIN", "0.02")       # require bb_width >= 0.02
    # atr fires, but bb_width 0.01 < 0.02 -> blocked
    vote = VolGateEntryStrategy().evaluate(_snap(atr=60.0, bb=0.01), None, MagicMock())
    assert vote is None
    # bb high enough -> fires
    vote2 = VolGateEntryStrategy().evaluate(_snap(atr=60.0, bb=0.05), None, MagicMock())
    assert vote2 is not None


def test_none_when_atr_or_price_missing(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    assert VolGateEntryStrategy().evaluate(_snap(atr=None), None, MagicMock()) is None
    assert VolGateEntryStrategy().evaluate(_snap(fut=None), None, MagicMock()) is None


def test_none_when_position_open(monkeypatch) -> None:
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    monkeypatch.setenv("ATR_ENTRY_MIN_PCT", "0.00088")
    vote = VolGateEntryStrategy().evaluate(_snap(atr=60.0), MagicMock(), MagicMock())
    assert vote is None
