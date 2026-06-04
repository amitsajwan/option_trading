from __future__ import annotations

import os

import pytest

from strategy_app.contracts import Direction
from strategy_app.market.depth_context import DepthContext, StrikeDepth
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.entry_direction_resolver import (
    resolve_entry_direction,
    resolve_entry_direction_composite,
    resolve_entry_direction_momentum,
)
from strategy_app.runtime.eval_context import clear_depth_context, set_depth_context


def _snap(**overrides: object) -> SnapshotAccessor:
    payload: dict = {
        "snapshot_id": "s1",
        "timestamp": "2024-08-15T06:00:00+00:00",
        "trade_date": "2024-08-15",
        "futures_derived": {"fut_return_5m": 0.003, "fut_return_15m": 0.002, "price_vs_vwap": 0.001},
        "opening_range": {"or_ready": True},
    }
    payload.update(overrides)
    return SnapshotAccessor(payload)


def test_momentum_picks_ce_on_positive_r5() -> None:
    result = resolve_entry_direction_momentum(_snap())
    assert result.direction == Direction.CE
    assert result.source == "momentum"


def test_composite_vetoes_when_no_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENTRY_DIR_MIN_MARGIN", "99")
    for key in (
        "ENTRY_DIR_W_MOMENTUM_5M",
        "ENTRY_DIR_W_MOMENTUM_15M",
        "ENTRY_DIR_W_VWAP",
        "ENTRY_DIR_W_VIX",
        "ENTRY_DIR_W_IV_SKEW",
        "ENTRY_DIR_W_OR_TRAP",
        "ENTRY_DIR_W_PCR",
        "ENTRY_DIR_W_DEPTH",
    ):
        monkeypatch.setenv(key, "0")
    snap = SnapshotAccessor(
        {
            "snapshot_id": "s1",
            "timestamp": "2024-08-15T06:00:00+00:00",
            "trade_date": "2024-08-15",
        }
    )
    result = resolve_entry_direction_composite(snap)
    assert result.vetoed
    assert result.direction is None


def test_composite_uses_depth_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENTRY_DIR_MIN_MARGIN", "0.1")
    monkeypatch.setenv("ENTRY_DIR_W_DEPTH", "2.0")
    monkeypatch.setenv("ENTRY_DIR_W_MOMENTUM_5M", "0.0")
    snap = _snap(futures_derived={})
    depth = DepthContext(
        ce=StrikeDepth(
            best_bid=100.0,
            best_ask=101.0,
            bid_qty=500,
            ask_qty=100,
            microprice=100.8,
            qty_imbalance=0.6,
        ),
    )
    set_depth_context(depth)
    try:
        result = resolve_entry_direction_composite(snap)
    finally:
        clear_depth_context()
    assert result.direction == Direction.CE
    assert any(k.startswith("depth_ce:") for k in result.sources)


def test_resolve_entry_direction_default_is_composite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ML_ENTRY_DIRECTION_MODE", raising=False)
    snap = _snap()
    result = resolve_entry_direction(snap)
    assert result.direction in (Direction.CE, Direction.PE)
    assert "composite" in result.source or result.source.startswith("composite")


def _depth_ctx_all_ce() -> DepthContext:
    """A book where bid_dom + imbalance + microprice ALL fire CE — the correlated
    multi-tick case that manufactured the fce59da2 margin."""
    return DepthContext(
        ce=StrikeDepth(
            best_bid=100.0,
            best_ask=101.0,
            bid_qty=500,
            ask_qty=100,     # bid >> ask -> bid_dom CE
            microprice=100.9,  # micro+ -> CE
            qty_imbalance=0.6,  # imb+ -> CE
        ),
    )


def test_depth_decorrelation_caps_net_vote(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate depth: kill every non-depth source so the score is depth-only.
    for key in (
        "ENTRY_DIR_W_MOMENTUM_5M", "ENTRY_DIR_W_MOMENTUM_15M", "ENTRY_DIR_W_VWAP",
        "ENTRY_DIR_W_VIX", "ENTRY_DIR_W_IV_SKEW", "ENTRY_DIR_W_OR_TRAP", "ENTRY_DIR_W_PCR",
    ):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("ENTRY_DIR_MIN_MARGIN", "0.01")
    monkeypatch.setenv("ENTRY_DIR_W_DEPTH", "1.1")
    monkeypatch.setenv("ENTRY_DIR_DEPTH_NET_CAP", "1.1")
    snap = _snap(futures_derived={})

    # Decorrelation ON (default): three correlated CE ticks collapse to <=1.1.
    monkeypatch.setenv("ENTRY_DIR_DEPTH_DECORRELATE", "1")
    set_depth_context(_depth_ctx_all_ce())
    try:
        on = resolve_entry_direction_composite(snap)
    finally:
        clear_depth_context()
    assert on.direction == Direction.CE
    assert on.margin <= 1.1 + 1e-9, f"net depth not capped: {on.margin}"
    assert "depth_net" in on.sources

    # Decorrelation OFF: the same ticks each add full weight -> much larger margin.
    monkeypatch.setenv("ENTRY_DIR_DEPTH_DECORRELATE", "0")
    set_depth_context(_depth_ctx_all_ce())
    try:
        off = resolve_entry_direction_composite(snap)
    finally:
        clear_depth_context()
    assert off.margin > on.margin, "decorrelation should reduce manufactured margin"
