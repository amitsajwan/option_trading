"""Verify the live fixes for the 8 previously-NaN entry features:

  - mongo-backed prev-day velocity context (ctx_gap_*, vol_spike_ratio,
    ctx_am_vol_vs_yday)
  - session-open VIX baseline (vix_intraday_chg)

Each test asserts the explicit input -> output mapping ("what goes in / what
goes out") and includes the no-context regression guard (stays NaN/None).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from snapshot_app.core.live_velocity_state import (
    LiveVelocityAccumulator,
    _load_context_from_mongo,
    make_mongo_context_provider,
)
from snapshot_app.core.market_snapshot import LiveMarketSnapshotBuilder, _compute_vix_block


# ───────────────────────── fake mongo ─────────────────────────

def _snap_doc(sid: str, *, ce_vol=None, pe_vol=None, fut_close=None) -> Dict[str, Any]:
    snap: Dict[str, Any] = {}
    if ce_vol is not None or pe_vol is not None:
        snap["chain_aggregates"] = {"total_ce_volume": ce_vol, "total_pe_volume": pe_vol}
    if fut_close is not None:
        snap["futures_bar"] = {"fut_close": fut_close}
    return {"snapshot_id": sid, "payload": {"snapshot": snap}}


class _FakeColl:
    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = docs

    @staticmethod
    def _match(docs, query):
        rx = query["snapshot_id"]["$regex"]
        pat = re.compile(rx)
        return [d for d in docs if pat.search(str(d.get("snapshot_id") or ""))]

    def find(self, query, projection=None):
        return list(self._match(self._docs, query))

    def find_one(self, query, projection=None, sort=None):
        rows = self._match(self._docs, query)
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda d: str(d.get(key) or ""), reverse=direction < 0)
        return rows[0] if rows else None


class _FakeDB:
    def __init__(self, colls: Dict[str, List[Dict[str, Any]]]) -> None:
        self._colls = {k: _FakeColl(v) for k, v in colls.items()}

    def __getitem__(self, name: str) -> _FakeColl:
        return self._colls.get(name, _FakeColl([]))


# ───────────────────────── mongo context loader ─────────────────────────

def test_mongo_context_loader_maps_inputs_to_outputs():
    # Three prior days, each with an 11:30 bar (ce+pe vol) and a last-of-day close.
    docs = [
        _snap_doc("20260609_1130", ce_vol=400, pe_vol=600),   # total 1000
        _snap_doc("20260609_1525", fut_close=50100.0),
        _snap_doc("20260610_1130", ce_vol=500, pe_vol=700),   # total 1200
        _snap_doc("20260610_1525", fut_close=50250.0),
        _snap_doc("20260611_1130", ce_vol=800, pe_vol=900),   # total 1700  (prev day)
        _snap_doc("20260611_1530", fut_close=50500.0),        # prev_day_close
        # a future day that must be ignored (>= trade_date)
        _snap_doc("20260612_1130", ce_vol=999, pe_vol=999),
    ]
    db = _FakeDB({"phase1_market_snapshots": docs})

    prev_close, prev_midday_vol, avg_20d = _load_context_from_mongo(
        db, "2026-06-12", collections=("phase1_market_snapshots",)
    )

    assert prev_close == 50500.0           # last-of-day close of the most recent prior day
    assert prev_midday_vol == 1700.0       # 11:30 ce+pe of the most recent prior day
    assert avg_20d == pytest.approx((1000 + 1200 + 1700) / 3)  # mean of all prior 11:30 totals


def test_mongo_context_loader_returns_none_when_no_history():
    db = _FakeDB({"phase1_market_snapshots": []})
    assert _load_context_from_mongo(db, "2026-06-12", collections=("phase1_market_snapshots",)) == (
        None, None, None,
    )


def test_mongo_context_loader_ignores_malformed_date():
    db = _FakeDB({"phase1_market_snapshots": [_snap_doc("20260611_1130", ce_vol=1, pe_vol=1)]})
    assert _load_context_from_mongo(db, "not-a-date", collections=("phase1_market_snapshots",)) == (
        None, None, None,
    )


def test_make_mongo_context_provider_is_callable():
    docs = [
        _snap_doc("20260611_1130", ce_vol=800, pe_vol=900),
        _snap_doc("20260611_1530", fut_close=50500.0),
    ]
    db = _FakeDB({"phase1_market_snapshots": docs, "phase1_market_snapshots_historical": []})
    provider = make_mongo_context_provider(db)
    prev_close, prev_midday_vol, avg_20d = provider("2026-06-12")
    assert prev_close == 50500.0
    assert prev_midday_vol == 1700.0
    assert avg_20d == 1700.0


# ───────────────────── accumulator prefers provider ─────────────────────

def test_accumulator_uses_context_provider_on_reset():
    acc = LiveVelocityAccumulator(
        context_provider=lambda td: (50500.0, 1700.0, 1300.0),
    )
    snap = {"trade_date": "2026-06-12", "timestamp": "2026-06-12T10:00:00+05:30"}
    acc.process(snap)  # first tick of a new trade_date triggers _reset -> provider
    assert acc._prev_day_close == 50500.0
    assert acc._prev_day_midday_vol == 1700.0
    assert acc._avg_20d_midday_vol == 1300.0


def test_accumulator_provider_failure_is_safe():
    def _boom(_td):
        raise RuntimeError("mongo down")

    acc = LiveVelocityAccumulator(context_provider=_boom)
    acc.process({"trade_date": "2026-06-12", "timestamp": "2026-06-12T10:00:00+05:30"})
    # No crash; context stays None -> features will be NaN (no regression).
    assert acc._prev_day_close is None
    assert acc._avg_20d_midday_vol is None


# ───────────────────────── vix session-open ─────────────────────────

def test_vix_intraday_chg_uses_session_open_when_daily_empty():
    out = _compute_vix_block(
        trade_date=pd.Timestamp("2026-06-12"),
        vix_daily=pd.DataFrame(),       # live: empty
        vix_live_current=15.0,
        session_open_vix=14.0,
    )
    assert out["vix_open"] == 14.0
    assert out["vix_intraday_chg"] == pytest.approx(((15.0 - 14.0) / 14.0) * 100.0)


def test_vix_intraday_chg_none_without_session_open():
    # Regression guard: prior behaviour (no baseline) -> still None, not a crash.
    out = _compute_vix_block(
        trade_date=pd.Timestamp("2026-06-12"),
        vix_daily=pd.DataFrame(),
        vix_live_current=15.0,
    )
    assert out["vix_intraday_chg"] is None


def test_builder_latches_session_open_vix_and_resets_on_new_day():
    b = LiveMarketSnapshotBuilder(instrument="BANKNIFTYFUT", enable_kite_backfill=False)
    day1 = pd.DataFrame([{"timestamp": "2026-06-12T09:15:00"}])

    b._update_session_open_vix(ohlc=day1, vix_live=14.5)
    assert b._session_open_vix == 14.5
    # later tick same day must NOT move the latched open
    b._update_session_open_vix(ohlc=day1, vix_live=16.2)
    assert b._session_open_vix == 14.5
    # new trade_date resets and re-latches
    day2 = pd.DataFrame([{"timestamp": "2026-06-13T09:15:00"}])
    b._update_session_open_vix(ohlc=day2, vix_live=13.1)
    assert b._session_open_vix == 13.1


def test_builder_session_open_vix_ignores_nonfinite():
    b = LiveMarketSnapshotBuilder(instrument="BANKNIFTYFUT", enable_kite_backfill=False)
    day1 = pd.DataFrame([{"timestamp": "2026-06-12T09:15:00"}])
    b._update_session_open_vix(ohlc=day1, vix_live=None)
    assert b._session_open_vix is None
    b._update_session_open_vix(ohlc=day1, vix_live=14.0)
    assert b._session_open_vix == 14.0
