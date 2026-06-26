"""
Broker pluggability guard (user directive 2026-06-26).

Swapping brokers (kite/zerodha/dhan/future) must be a registry + config change only —
never an app-code change. These tests lock that contract: selection is by the explicit
BROKER env (with a legacy fallback), the registry is the single place a broker is added,
and an unknown broker fails loudly.
"""

from __future__ import annotations

import pytest

from ingestion_app import api_service as a


def test_explicit_broker_env_selects(monkeypatch):
    monkeypatch.setenv("BROKER", "dhan")
    assert a._resolve_broker() == "dhan"
    monkeypatch.setenv("BROKER", "zerodha")
    assert a._resolve_broker() == "zerodha"
    monkeypatch.setenv("BROKER", "KITE")  # case-insensitive
    assert a._resolve_broker() == "kite"


def test_legacy_fallback_when_broker_unset(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    assert a._resolve_broker() == "dhan"           # legacy: token present -> dhan
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    assert a._resolve_broker() == "kite"           # default -> kite


def test_explicit_broker_wins_over_legacy_token(monkeypatch):
    monkeypatch.setenv("BROKER", "kite")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    assert a._resolve_broker() == "kite"


def test_registry_is_the_single_extension_point():
    # Adding a broker = one line here. These must always be present.
    for name in ("kite", "zerodha", "dhan"):
        assert name in a._MARKET_DATA_SERVICES
        assert callable(a._MARKET_DATA_SERVICES[name])


def test_unknown_broker_fails_loudly(monkeypatch):
    monkeypatch.setenv("BROKER", "robinhood")
    with pytest.raises(ValueError):
        a._build_svc()
