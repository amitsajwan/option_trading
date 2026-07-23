"""exit_premium wiring (found 2026-07-22 review).

Before the fix, TradeSignal only had entry_premium -- _close_position() always
set it to the ORIGINAL entry price even on an exit signal, so there was no
channel for the real exit price to reach execution_app. PaperAdapter.place_exit's
fallback chain (signal.entry_premium or position.current_premium or ...) always
resolved to the entry price, so every paper-mode exit "filled" at entry and
paper P&L was silently always ~0% -- meaningless for gating live promotion.
"""
from __future__ import annotations

from execution_app.adapter.paper import PaperAdapter
from execution_app.consumer import _dict_to_position_stub, _dict_to_signal_stub


def test_dict_to_signal_stub_carries_exit_premium():
    stub = _dict_to_signal_stub({"entry_premium": 100.0, "exit_premium": 65.0})
    assert stub.entry_premium == 100.0
    assert stub.exit_premium == 65.0


def test_dict_to_position_stub_current_premium_uses_exit_premium():
    stub = _dict_to_position_stub({"entry_premium": 100.0, "exit_premium": 65.0})
    assert stub.entry_premium == 100.0
    assert stub.current_premium == 65.0


def test_dict_to_position_stub_falls_back_to_entry_when_exit_premium_absent():
    """Old trace/signal records (pre-fix) won't have exit_premium at all."""
    stub = _dict_to_position_stub({"entry_premium": 100.0})
    assert stub.current_premium == 100.0


def test_paper_adapter_exit_fills_at_exit_premium_not_entry():
    adapter = PaperAdapter()
    signal = _dict_to_signal_stub({
        "signal_id": "s1", "direction": "CE", "strike": 54600,
        "entry_premium": 100.0, "exit_premium": 65.0, "max_lots": 1,
    })
    position = _dict_to_position_stub({
        "position_id": "p1", "entry_premium": 100.0, "exit_premium": 65.0, "max_lots": 1,
    })
    result = adapter.place_exit(signal, position)
    assert result.fill_price == 65.0


def test_paper_adapter_exit_zero_premium_is_not_treated_as_missing():
    """A worthless expired option exiting at 0 must fill at 0, not fall through
    to entry_premium via a falsy-zero bug."""
    adapter = PaperAdapter()
    signal = _dict_to_signal_stub({
        "signal_id": "s1", "direction": "CE", "strike": 54600,
        "entry_premium": 100.0, "exit_premium": 0.0, "max_lots": 1,
    })
    position = _dict_to_position_stub({
        "position_id": "p1", "entry_premium": 100.0, "exit_premium": 0.0, "max_lots": 1,
    })
    result = adapter.place_exit(signal, position)
    assert result.fill_price == 0.0
