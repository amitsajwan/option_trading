"""Trader-suggestion changes (2026-06-04 postmortem):

  #1 Trend-aware regime: REGIME_TREND_ALIGNED_BONUS / REGIME_TREND_SCORE_MIN let a
     cleanly-aligned but low-volume grind-trend clear the TRENDING threshold instead
     of being demoted to SIDEWAYS (which would route the adaptive exit to the scalper
     3% target instead of the runner stack).

  #3 Trend-fade guard: TREND_FADE_GUARD_ENABLED blocks counter-trend option entries
     that fade the dominant VWAP trend on a shallow pullback, while releasing once the
     opposing move becomes a genuine 30m trend.

All knobs default to legacy behaviour (bonus 0.0 / threshold 2.0 / guard off).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from strategy_app.contracts import Direction, SignalType, StrategyVote
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.market.regime import Regime, RegimeClassifier
from strategy_app.market.snapshot_accessor import SnapshotAccessor


# ---------------------------------------------------------------------------
# #1 — trend-aware regime
# ---------------------------------------------------------------------------


def _regime_snap(*, r5m, r15m, r30m, vol_ratio) -> SnapshotAccessor:
    return SnapshotAccessor({
        "snapshot_id": "s1",
        "session_context": {
            "date": "2026-06-04", "is_expiry_day": False, "vix_spike_flag": False,
            "days_to_expiry": 3, "is_pre_close": False,
        },
        "futures_bar": {"close": 54400.0, "high": 54600.0, "low": 54200.0},
        "futures_derived": {
            "fut_return_5m": r5m, "fut_return_15m": r15m, "fut_return_30m": r30m,
            "vol_ratio": vol_ratio, "realized_vol_30m": 0.008,
            "fut_oi": 1_000_000.0, "fut_oi_change_30m": 5_000.0,
        },
        "vix_context": {"vix_current": 15.0, "vix_intraday_chg": 0.5,
                         "vix_spike_flag": False, "vix_regime": "normal"},
        "chain_aggregates": {"pcr": 1.05},
        "atm_options": {"atm_ce_ltp": 120.0, "atm_pe_ltp": 118.0},
        "opening_range": {"ready": True, "orh": 54600.0, "orl": 54200.0,
                          "orh_broken": False, "orl_broken": False},
        "iv_derived": {"iv_percentile": 45.0, "iv_regime": "normal"},
        "session_levels": {},
    })


# An aligned-up, low-volume grind (mirrors 2026-06-04 ~09:50–10:08).
_ALIGNED_WEAK_VOL = dict(r5m=0.0015, r15m=0.002, r30m=0.0015, vol_ratio=0.85)


def test_aligned_weak_vol_defaults_to_sideways(monkeypatch):
    monkeypatch.delenv("REGIME_TREND_ALIGNED_BONUS", raising=False)
    monkeypatch.delenv("REGIME_TREND_SCORE_MIN", raising=False)
    result = RegimeClassifier().classify(_regime_snap(**_ALIGNED_WEAK_VOL))
    assert result.regime == Regime.SIDEWAYS


def test_aligned_bonus_promotes_to_trending(monkeypatch):
    monkeypatch.setenv("REGIME_TREND_ALIGNED_BONUS", "0.8")
    result = RegimeClassifier().classify(_regime_snap(**_ALIGNED_WEAK_VOL))
    assert result.regime == Regime.TRENDING


def test_lower_threshold_promotes_to_trending(monkeypatch):
    monkeypatch.delenv("REGIME_TREND_ALIGNED_BONUS", raising=False)
    monkeypatch.setenv("REGIME_TREND_SCORE_MIN", "1.0")
    result = RegimeClassifier().classify(_regime_snap(**_ALIGNED_WEAK_VOL))
    assert result.regime == Regime.TRENDING


def test_mixed_returns_not_promoted_to_trending_by_bonus(monkeypatch):
    # Bonus only applies to a genuinely aligned side; mixed returns must NOT become a
    # (false) trend — they stay in the non-trending bucket (SIDEWAYS/CHOP).
    monkeypatch.setenv("REGIME_TREND_ALIGNED_BONUS", "0.8")
    result = RegimeClassifier().classify(
        _regime_snap(r5m=0.0015, r15m=-0.0015, r30m=0.0005, vol_ratio=0.85)
    )
    assert result.regime != Regime.TRENDING
    assert result.regime in (Regime.SIDEWAYS, Regime.CHOP)


# ---------------------------------------------------------------------------
# #3 — trend-fade guard
# ---------------------------------------------------------------------------


def _fade_snap(*, price_vs_vwap, r30m) -> SnapshotAccessor:
    return SnapshotAccessor({
        "snapshot_id": "s1",
        "timestamp": datetime(2026, 6, 4, 10, 33, tzinfo=timezone.utc),
        "trade_date_ist": "2026-06-04",
        "futures_bar": {"fut_close": 54432.0},
        "futures_derived": {"price_vs_vwap": price_vs_vwap, "fut_return_30m": r30m},
        "chain_aggregates": {"atm_strike": 54400},
        "strikes": [{"strike": 54400, "ce_ltp": 100.0, "pe_ltp": 100.0}],
    })


def _vote(direction: Direction) -> StrategyVote:
    return StrategyVote(
        strategy_name="ML_ENTRY", snapshot_id="s1",
        timestamp=datetime(2026, 6, 4, 10, 33, tzinfo=timezone.utc),
        trade_date="2026-06-04", signal_type=SignalType.ENTRY,
        direction=direction, confidence=0.8, reason="test",
    )


def _block(votes, snap):
    # helper is self-independent; call unbound with self=None
    return DeterministicRuleEngine._trend_fade_block(None, votes, snap)


def test_fade_guard_off_by_default(monkeypatch):
    monkeypatch.delenv("TREND_FADE_GUARD_ENABLED", raising=False)
    snap = _fade_snap(price_vs_vwap=0.0012, r30m=-0.0026)
    assert _block([_vote(Direction.PE)], snap) is None


def test_fade_guard_blocks_pe_above_vwap_shallow_pullback(monkeypatch):
    # 2026-06-04 10:33: price +0.1% above VWAP, only a shallow 30m dip → don't buy puts.
    monkeypatch.setenv("TREND_FADE_GUARD_ENABLED", "true")
    snap = _fade_snap(price_vs_vwap=0.0012, r30m=-0.0026)
    assert _block([_vote(Direction.PE)], snap) == "trend_fade_guard:PE"


def test_fade_guard_releases_on_real_downtrend(monkeypatch):
    # Once the down-move is a genuine 30m trend, the PE reversal is allowed.
    monkeypatch.setenv("TREND_FADE_GUARD_ENABLED", "true")
    snap = _fade_snap(price_vs_vwap=0.0012, r30m=-0.008)
    assert _block([_vote(Direction.PE)], snap) is None


def test_fade_guard_does_not_block_with_trend_ce(monkeypatch):
    # CE while price is above VWAP is WITH the trend — never blocked.
    monkeypatch.setenv("TREND_FADE_GUARD_ENABLED", "true")
    snap = _fade_snap(price_vs_vwap=0.0012, r30m=-0.0026)
    assert _block([_vote(Direction.CE)], snap) is None


def test_fade_guard_blocks_ce_below_vwap_shallow_bounce(monkeypatch):
    monkeypatch.setenv("TREND_FADE_GUARD_ENABLED", "true")
    snap = _fade_snap(price_vs_vwap=-0.0012, r30m=0.0026)
    assert _block([_vote(Direction.CE)], snap) == "trend_fade_guard:CE"


def test_fade_guard_noop_when_vwap_data_missing(monkeypatch):
    monkeypatch.setenv("TREND_FADE_GUARD_ENABLED", "true")
    snap = _fade_snap(price_vs_vwap=None, r30m=-0.0026)
    assert _block([_vote(Direction.PE)], snap) is None
