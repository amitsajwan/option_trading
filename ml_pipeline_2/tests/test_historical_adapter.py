"""
Broker pluggability guard for the historical/training fetch (user directive 2026-06-26).

Mirrors the live-feed broker registry: swapping the historical data broker must be a
registry + config change only. The fetch adapter is selected by BROKER; adding a broker
= implement HistoricalDataAdapter + one registry line. The build/assemble steps and
feature_engine stay broker-agnostic.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

import pytest

_PIPELINE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "ml_pipeline_2" / "scripts" / "dhan_data_pipeline.py"
)
_spec = importlib.util.spec_from_file_location("dhan_data_pipeline_for_test", _PIPELINE)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod  # @dataclass needs the module in sys.modules during exec
_spec.loader.exec_module(mod)  # safe: main() is __main__-guarded


def _args(broker: str = "") -> argparse.Namespace:
    return argparse.Namespace(token="dummy", client_id="1111957145", broker=broker, rps=4.0)


def test_dhan_adapter_registered_and_conforms():
    assert "dhan" in mod._HISTORICAL_ADAPTERS
    assert issubclass(mod.DhanHistoricalAdapter, mod.HistoricalDataAdapter)


def test_default_resolves_to_dhan(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    adapter = mod.build_historical_adapter(_args())
    assert isinstance(adapter, mod.DhanHistoricalAdapter)


def test_broker_env_selects(monkeypatch):
    monkeypatch.setenv("BROKER", "dhan")
    assert isinstance(mod.build_historical_adapter(_args()), mod.DhanHistoricalAdapter)


def test_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("BROKER", "kite")          # not implemented for historical yet
    # explicit --broker dhan overrides the env
    assert isinstance(mod.build_historical_adapter(_args(broker="dhan")), mod.DhanHistoricalAdapter)


def test_unknown_broker_fails_loudly(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    with pytest.raises(ValueError):
        mod.build_historical_adapter(_args(broker="robinhood"))


def test_adapter_interface_methods_present():
    # The interface the build step depends on — a new broker must implement all of these.
    for name in ("validate", "fetch_index", "fetch_vix", "fetch_futures", "fetch_option"):
        assert hasattr(mod.DhanHistoricalAdapter, name)


# ── Instrument-aware expiry cadence (P2) ───────────────────────────────────────

import datetime as _dt


def _weekdays(start: _dt.date, n: int) -> list:
    return [start + _dt.timedelta(d) for d in range(n) if (start + _dt.timedelta(d)).weekday() < 5]


def test_instrument_config_cadence():
    assert mod.INSTRUMENTS["NIFTY"].expiry_cadence == "weekly"
    assert mod.INSTRUMENTS["BANKNIFTY"].expiry_cadence == "monthly"


def test_weekly_expiry_cadence():
    days = _weekdays(_dt.date(2024, 11, 1), 120)
    wk = mod._build_expiry_dates(days, "weekly")
    assert mod._expiry_for(_dt.date(2024, 11, 7), wk) == _dt.date(2024, 11, 7)   # Thu -> DTE 0
    assert mod._expiry_for(_dt.date(2024, 11, 8), wk) == _dt.date(2024, 11, 14)  # Fri -> next Thu
    for td in days[:60]:
        dte = (mod._expiry_for(td, wk) - td).days
        assert 0 <= dte <= 7, f"weekly DTE out of range at {td}: {dte}"


def test_monthly_expiry_cadence():
    days = _weekdays(_dt.date(2024, 11, 1), 120)
    mo = mod._build_expiry_dates(days, "monthly")
    assert mod._expiry_for(_dt.date(2024, 11, 28), mo) == _dt.date(2024, 11, 28)  # last Thu Nov
    dte = (mod._expiry_for(_dt.date(2024, 11, 5), mo) - _dt.date(2024, 11, 5)).days
    assert 20 <= dte <= 31, f"monthly DTE unexpected: {dte}"
