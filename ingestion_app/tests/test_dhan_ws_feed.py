"""Tests for DhanWsFeed — fully mocked, no live WS or network."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from ingestion_app.dhan_ws_feed import DhanWsFeed, _HB_KEY, _HB_TTL_S, _TICK_KEY


def _make_feed(futures_sid="62326") -> tuple[DhanWsFeed, MagicMock]:
    redis_mock = MagicMock()
    redis_mock.get.return_value = None
    feed = DhanWsFeed(
        client_id="1111",
        access_token="tok",
        futures_security_id=futures_sid,
        redis_client=redis_mock,
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


# ── _on_message tick parsing + Redis publish ─────────────────────────────────

def test_on_message_publishes_banknifty_index(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
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


def test_on_message_publishes_vix_tick(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
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


def test_on_message_publishes_futures_tick(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
    feed, redis_mock = _make_feed(futures_sid="62326")
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


def test_on_message_ignores_non_dict():
    feed, redis_mock = _make_feed()
    feed._on_message(None, "not-a-dict")   # must not raise
    redis_mock.pipeline.assert_not_called()


# ── sid_to_label ─────────────────────────────────────────────────────────────

def test_sid_to_label_vix(monkeypatch):
    feed, _ = _make_feed()
    assert feed._sid_to_label("21", 0) == "INDIAVIX"


def test_sid_to_label_banknifty_idx(monkeypatch):
    feed, _ = _make_feed()
    assert feed._sid_to_label("25", 0) == "BANKNIFTY"


def test_sid_to_label_futures(monkeypatch):
    monkeypatch.setenv("INSTRUMENT_SYMBOL", "BANKNIFTY26JULFUT")
    feed, _ = _make_feed()
    assert feed._sid_to_label("62326", 2) == "BANKNIFTY26JULFUT"


# ── update_token ─────────────────────────────────────────────────────────────

def test_update_token():
    feed, _ = _make_feed()
    feed.update_token("new-token-xyz")
    assert feed._token == "new-token-xyz"
