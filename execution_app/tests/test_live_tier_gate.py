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


def _raw(signal_type: str, tier, *, signal_id="s1", run_id=None) -> str:
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
    if run_id is not None:
        sig["run_id"] = run_id
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
    # Full-live mode: every (non-sim) signal reaches the broker regardless of tier.
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "0")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "paper"))
    adapter.place_entry.assert_called_once()


# ── Non-bypassable sim block (closes the 2026-06-14 sim→Dhan leak) ───────────

@pytest.mark.parametrize("run_id", ["sim-2026-06-12", "sim-2026-05-26", "SIM-foo"])
def test_sim_signal_blocked_even_with_live_tier(monkeypatch, run_id):
    # A sim run_id must NOT execute even if mis-tagged tier=="live".
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "live", run_id=run_id))
    adapter.place_entry.assert_not_called()


def test_sim_signal_blocked_even_when_tier_gate_disabled(monkeypatch):
    # The leak scenario: full-live mode (gate off) + a sim signal → must STILL block.
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "0")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "live", run_id="sim-2026-06-12"))
    adapter.place_entry.assert_not_called()


def test_sim_exit_blocked(monkeypatch):
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "0")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("EXIT", "live", run_id="sim-2026-06-12"))
    adapter.place_exit.assert_not_called()


def test_live_signal_with_null_run_id_still_executes(monkeypatch):
    # Genuine live (run_id absent/null) must be unaffected by the sim guard.
    monkeypatch.setenv("EXECUTION_REQUIRE_LIVE_TIER", "1")
    adapter = mock.MagicMock()
    consumer = _make_consumer(monkeypatch, adapter)
    consumer._handle_message(_raw("ENTRY", "live", run_id=None))
    adapter.place_entry.assert_called_once()
