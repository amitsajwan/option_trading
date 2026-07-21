"""Tests for DhanWsFeed — fully mocked, no live WS or network."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from ingestion_app.dhan_ws_feed import (
    DhanWsFeed,
    _HB_KEY,
    _HB_TTL_S,
    _TICK_KEY,
    build_shared_legs,
)

_BANKNIFTY_IDX_LEG = {"segment": 0, "security_id": "25", "mode": 17, "label": "BANKNIFTY"}
_VIX_LEG = {"segment": 0, "security_id": "21", "mode": 15, "label": "INDIAVIX"}
_BANKNIFTY_FUT_LEG = {"segment": 2, "security_id": "62326", "mode": 17, "label": "BANKNIFTY26JULFUT"}


def _make_feed(legs=None) -> tuple[DhanWsFeed, MagicMock]:
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    feed = DhanWsFeed(
        client_id="1111",
        access_token="tok",
        redis_client=redis_mock,
        legs=legs if legs is not None else [_VIX_LEG, _BANKNIFTY_IDX_LEG, _BANKNIFTY_FUT_LEG],
    )
    return feed, redis_mock


# ── should_enable ────────────────────────────────────────────────────────────

def test_should_enable_default(monkeypatch):
    monkeypatch.delenv("DHAN_WS_ENABLED", raising=False)
    assert DhanWsFeed.should_enable() is True


def test_should_enable_disabled(monkeypatch):
    monkeypatch.setenv("DHAN_WS_ENABLED", "0")
    assert DhanWsFeed.should_enable() is False


# ── heartbeat / is_healthy ───────────────────────────────────────────────────

def test_is_healthy_true_when_recent_heartbeat():
    feed, redis_mock = _make_feed()
    redis_mock.get.return_value = str(time.time() - 5)   # 5s ago < TTL
    assert feed.is_healthy() is True


def test_is_healthy_false_when_stale_heartbeat():
    feed, redis_mock = _make_feed()
    redis_mock.get.return_value = str(time.time() - (_HB_TTL_S + 10))
    assert feed.is_healthy() is False


def test_is_healthy_false_when_no_heartbeat():
    feed, redis_mock = _make_feed()
    redis_mock.get.return_value = None
    assert feed.is_healthy() is False


# ── get_cached_tick ──────────────────────────────────────────────────────────

def test_get_cached_tick_returns_none_when_stale():
    feed, redis_mock = _make_feed()
    redis_mock.get.return_value = None   # no heartbeat → stale
    result = feed.get_cached_tick("websocket:tick:BANKNIFTY:latest")
    assert result is None


def test_get_cached_tick_returns_dict_when_healthy():
    feed, redis_mock = _make_feed()
    tick_data = {"instrument": "BANKNIFTY", "last_price": 58400.0}

    def _redis_get(key):
        if key == _HB_KEY:
            return str(time.time() - 1)       # fresh heartbeat
        return json.dumps(tick_data)

    redis_mock.get.side_effect = _redis_get
    result = feed.get_cached_tick("websocket:tick:BANKNIFTY:latest")
    assert result["last_price"] == 58400.0


# ── start() / no legs (non-owner) ────────────────────────────────────────────

def test_start_with_no_legs_does_not_launch_thread():
    feed, _ = _make_feed(legs=[])
    feed.start()
    assert feed._thread is None


def test_start_with_legs_launches_thread():
    feed, _ = _make_feed()
    with patch.object(DhanWsFeed, "_run_loop", lambda self: None):
        feed.start()
        assert feed._thread is not None


# ── _on_message tick parsing + Redis publish ─────────────────────────────────

def test_on_message_publishes_banknifty_index():
    feed, redis_mock = _make_feed()
    pipe_mock = MagicMock()
    redis_mock.pipeline.return_value = pipe_mock

    msg = {
        "type": "Quote_Data",
        "exchange_segment": 0,    # IDX_I
        "security_id": 25,        # BankNifty index
        "LTP": 58400.5,
        "volume": 10000,
        "OI": 0,
        "top_seller": [{"bid_price": 58399.0}],
        "top_buyer":  [{"ask_price": 58402.0}],
    }
    feed._on_message(None, msg)

    calls = [str(c) for c in pipe_mock.set.call_args_list]
    assert any("websocket:tick:BANKNIFTY:latest" in c for c in calls)
    assert any("58400.5" in c for c in calls)
    pipe_mock.execute.assert_called_once()


def test_on_message_publishes_vix_tick():
    feed, redis_mock = _make_feed()
    pipe_mock = MagicMock()
    redis_mock.pipeline.return_value = pipe_mock

    msg = {
        "exchange_segment": 0,
        "security_id": 21,      # India VIX
        "LTP": 14.75,
    }
    feed._on_message(None, msg)
    calls = [str(c) for c in pipe_mock.set.call_args_list]
    assert any("INDIAVIX" in c for c in calls)


def test_on_message_publishes_futures_tick():
    feed, redis_mock = _make_feed()
    pipe_mock = MagicMock()
    redis_mock.pipeline.return_value = pipe_mock

    msg = {
        "exchange_segment": 2,  # NSE_FNO
        "security_id": 62326,
        "LTP": 58410.0,
        "OI": 5000,
        "volume": 800,
    }
    feed._on_message(None, msg)
    calls = [str(c) for c in pipe_mock.set.call_args_list]
    assert any("BANKNIFTY26JULFUT" in c for c in calls)


def test_on_message_ignores_unregistered_leg():
    """A message for a security_id not in this instance's legs must not publish."""
    feed, redis_mock = _make_feed()
    msg = {"exchange_segment": 2, "security_id": 99999, "LTP": 1.0}
    feed._on_message(None, msg)
    redis_mock.pipeline.assert_not_called()


def test_on_message_ignores_non_dict():
    feed, redis_mock = _make_feed()
    feed._on_message(None, "not-a-dict")   # must not raise
    redis_mock.pipeline.assert_not_called()


# ── sid_to_label ─────────────────────────────────────────────────────────────

def test_sid_to_label_vix():
    feed, _ = _make_feed()
    assert feed._sid_to_label("21", 0) == "INDIAVIX"


def test_sid_to_label_banknifty_idx():
    feed, _ = _make_feed()
    assert feed._sid_to_label("25", 0) == "BANKNIFTY"


def test_sid_to_label_futures():
    feed, _ = _make_feed()
    assert feed._sid_to_label("62326", 2) == "BANKNIFTY26JULFUT"


def test_sid_to_label_unregistered_returns_none():
    feed, _ = _make_feed()
    assert feed._sid_to_label("99999", 2) is None


# ── update_token ─────────────────────────────────────────────────────────────

def test_update_token():
    feed, _ = _make_feed()
    feed.update_token("new-token-xyz")
    assert feed._token == "new-token-xyz"


# ── build_shared_legs ────────────────────────────────────────────────────────

def _scrip_master_stub(rows_by_underlying):
    stub = MagicMock()
    stub.find_nearest_futures.side_effect = lambda name: rows_by_underlying.get(name)
    return stub


def test_build_shared_legs_covers_both_instruments(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
    monkeypatch.setenv("NIFTY_INSTRUMENT_SYMBOL", "NIFTY26JULFUT")
    scrip = _scrip_master_stub({
        "BANKNIFTY": {"SEM_SMST_SECURITY_ID": "62326"},
        "NIFTY": {"SEM_SMST_SECURITY_ID": "77001"},
    })

    legs = build_shared_legs(scrip)
    by_label = {leg["label"]: leg for leg in legs}

    assert by_label["INDIAVIX"]["security_id"] == "21"
    assert by_label["BANKNIFTY"]["security_id"] == "25"
    assert by_label["NIFTY"]["security_id"] == "13"
    # Regression guard for the 2026-07-21 bug: each underlying's futures leg
    # must resolve to ITS OWN security id, not both silently pointing at
    # BankNifty's (the old hardcoded-"BANKNIFTY" constructor argument bug).
    assert by_label["BANKNIFTY26JULFUT"]["security_id"] == "62326"
    assert by_label["NIFTY26JULFUT"]["security_id"] == "77001"
    assert by_label["BANKNIFTY26JULFUT"]["security_id"] != by_label["NIFTY26JULFUT"]["security_id"]


def test_build_shared_legs_skips_futures_leg_when_scrip_lookup_fails(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
    monkeypatch.setenv("NIFTY_INSTRUMENT_SYMBOL", "NIFTY26JULFUT")
    scrip = _scrip_master_stub({"BANKNIFTY": {"SEM_SMST_SECURITY_ID": "62326"}, "NIFTY": None})

    legs = build_shared_legs(scrip)
    labels = {leg["label"] for leg in legs}

    assert "BANKNIFTY26JULFUT" in labels
    assert "NIFTY26JULFUT" not in labels          # skipped, not silently wrong
    assert "NIFTY" in labels                       # index leg unaffected


def test_build_shared_legs_skips_futures_leg_when_env_missing(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
    monkeypatch.delenv("NIFTY_INSTRUMENT_SYMBOL", raising=False)
    scrip = _scrip_master_stub({
        "BANKNIFTY": {"SEM_SMST_SECURITY_ID": "62326"},
        "NIFTY": {"SEM_SMST_SECURITY_ID": "77001"},
    })

    legs = build_shared_legs(scrip)
    labels = {leg["label"] for leg in legs}
    assert "NIFTY26JULFUT" not in labels
