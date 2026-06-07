"""Tests for deterministic market reference levels + the provider (no network)."""

from __future__ import annotations

import json
from datetime import date

from strategy_app.brain.providers.market_context import MarketContextProvider
from strategy_app.brain.providers.market_levels import (
    compute_levels,
    load_daily_ohlc,
    prefixed_levels,
)

_OHLC = [
    {"date": "2026-06-01", "open": 53800, "high": 54050, "low": 53700, "close": 53950},
    {"date": "2026-06-02", "open": 53950, "high": 54300, "low": 53900, "close": 54250},
    {"date": "2026-06-03", "open": 54250, "high": 54400, "low": 54100, "close": 54180},
    {"date": "2026-06-04", "open": 54180, "high": 54200, "low": 53600, "close": 53700},
    {"date": "2026-06-05", "open": 53700, "high": 53900, "low": 53400, "close": 53500},
]


class TestComputeLevels:
    def test_prev_day_and_week(self):
        lv = compute_levels(_OHLC, asof=date(2026, 6, 8))  # Monday after
        assert lv["prev_day_date"] == "2026-06-05"
        assert lv["prev_day_high"] == 53900
        assert lv["prev_day_low"] == 53400
        assert lv["prev_day_close"] == 53500
        # recent-week (5 sessions) high/low across the window
        assert lv["recent_week_high"] == 54400
        assert lv["recent_week_low"] == 53400
        assert lv["week_sessions"] == 5
        # week return: (53500 - 53800) / 53800 < 0
        assert lv["recent_week_return_pct"] < 0

    def test_asof_excludes_same_and_future_days(self):
        # asof on 06-03 => only 06-01, 06-02 are "before"
        lv = compute_levels(_OHLC, asof=date(2026, 6, 3))
        assert lv["prev_day_date"] == "2026-06-02"
        assert lv["recent_week_high"] == 54300

    def test_empty_and_malformed(self):
        assert compute_levels([], asof=date(2026, 6, 8)) == {}
        assert compute_levels([{"date": "bad"}], asof=date(2026, 6, 8)) == {}
        # missing high/low/close skipped
        assert compute_levels([{"date": "2026-06-01", "open": 1}], asof=date(2026, 6, 8)) == {}

    def test_unsorted_input_is_handled(self):
        shuffled = [_OHLC[2], _OHLC[0], _OHLC[4], _OHLC[1], _OHLC[3]]
        assert compute_levels(shuffled, asof=date(2026, 6, 8))["prev_day_date"] == "2026-06-05"

    def test_prefixed(self):
        out = prefixed_levels(_OHLC, asof=date(2026, 6, 8))
        assert "market.prev_day_high" in out
        assert all(k.startswith("market.") for k in out)


class TestLoader:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_daily_ohlc(tmp_path / "nope.json") == []

    def test_list_format(self, tmp_path):
        p = tmp_path / "ohlc.json"
        p.write_text(json.dumps(_OHLC), encoding="utf-8")
        assert len(load_daily_ohlc(p)) == 5

    def test_date_keyed_dict_format(self, tmp_path):
        p = tmp_path / "ohlc.json"
        keyed = {r["date"]: {k: v for k, v in r.items() if k != "date"} for r in _OHLC}
        p.write_text(json.dumps(keyed), encoding="utf-8")
        recs = load_daily_ohlc(p)
        assert len(recs) == 5 and all("date" in r for r in recs)

    def test_corrupt_file_returns_empty(self, tmp_path):
        p = tmp_path / "ohlc.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_daily_ohlc(p) == []

    def test_dict_key_is_authoritative_date(self, tmp_path):
        # a stale 'date' inside the value must not override the dict key
        p = tmp_path / "ohlc.json"
        p.write_text(json.dumps({
            "2026-06-05": {"date": "1999-01-01", "high": 100, "low": 90, "close": 95},
        }), encoding="utf-8")
        recs = load_daily_ohlc(p)
        assert recs[0]["date"] == "2026-06-05"


class TestProvider:
    def test_emits_market_keys(self, tmp_path):
        p = tmp_path / "ohlc.json"
        p.write_text(json.dumps(_OHLC), encoding="utf-8")
        out = MarketContextProvider(path=p).provide(date(2026, 6, 8))
        assert out["market.prev_day_close"] == 53500
        assert "market.recent_week_high" in out

    def test_absent_file_is_noop(self, tmp_path):
        assert MarketContextProvider(path=tmp_path / "nope.json").provide(date(2026, 6, 8)) == {}
