"""Paper/live safety gate in the execution consumer.

The strategy publishes BOTH paper- and live-tier signals to the same topic. Only
tier=="live" signals may reach the real broker. The gate is fail-closed: a missing or
paper tier is skipped for BOTH entries and exits (skip a paper entry → no unwanted real
order; skip a paper exit → no naked "sell-to-close" on an option never bought). Disabled
via EXECUTION_REQUIRE_LIVE_TIER=0.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from contracts_app import build_trade_signal_event


def _make_consumer(monkeypatch, adapter):
    # Patch redis so ExecutionConsumer construction needs no server.
    with mock.patch("execution_app.consumer.redis.Redis", return_value=mock.MagicMock()):
        from execution_app.consumer import ExecutionConsumer
        consumer = ExecutionConsumer(adapter)
    # Replace the order manager so a live path doesn't poll a real broker.
    consumer._order_manager = mock.MagicMock()
    consumer._order_manager.place_and_confirm.return_value = None  # skip _emit_fill branch
    consumer._r = mock.MagicMock()
    return consumer


def _raw(signal_type: str, tier, *, signal_id="s1") -> str:
    sig = {
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": "CE",
        "strike": 54600,
        "entry_premium": 1000.0,
        "max_lots": 1,
        "position_id": "p1" if signal_type == "EXIT" else None,
    }
    if tier is not None:
        sig["tier"] = tier
    return json.dumps(build_trade_signal_event(signal=sig, source="strategy_app"))


@pytest.mark.parametrize("tier", ["paper", None, "", "PAPER "])
def test_gate_skips_non_live_entry(monkeypatch, tier):
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", tier))
    adapter.place_entry.assert_not_called()


def test_gate_skips_non_live_exit(monkeypatch):
    # A paper position's exit must NOT hit the broker (would be a naked short).
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("EXIT", "paper"))
    adapter.place_exit.assert_not_called()


def test_gate_allows_live_entry(monkeypatch):
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "live"))
    adapter.place_entry.assert_called_once()


def test_gate_allows_live_exit(monkeypatch):
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("EXIT", "live"))
    adapter.place_exit.assert_called_once()


def test_gate_disabled_executes_everything(monkeypatch):
    # Full-live mode: every signal reaches the broker regardless of tier.
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "0")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "paper"))
    adapter.place_entry.assert_called_once()
