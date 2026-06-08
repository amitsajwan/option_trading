from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from strategy_app.contracts import Direction, SignalType, StrategyVote
from strategy_app.engines.direction_consensus import resolve_direction_consensus
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _snap(**overrides: object) -> SnapshotAccessor:
    payload = {
        "snapshot_id": "s1",
        "timestamp": datetime(2024, 5, 2, 10, 0, tzinfo=timezone.utc),
        "trade_date_ist": "2024-05-02",
        "futures_bar": {"fut_close": 50000.0, "fut_return_5m": 0.001},
        "chain_aggregates": {"atm_strike": 50000},
        "strikes": [{"strike": 50000, "ce_ltp": 100.0, "pe_ltp": 100.0}],
    }
    payload.update(overrides)
    return SnapshotAccessor(payload)


def _vote(name: str, direction: Direction, confidence: float = 0.8) -> StrategyVote:
    return StrategyVote(
        strategy_name=name,
        snapshot_id="s1",
        timestamp=datetime(2024, 5, 2, 10, 0, tzinfo=timezone.utc),
        trade_date="2024-05-02",
        signal_type=SignalType.ENTRY,
        direction=direction,
        confidence=confidence,
        reason="test",
    )


def test_consensus_vetoes_when_margin_too_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "5.0")
    snap = _snap()
    result = resolve_direction_consensus(
        snap=snap,
        rule_votes=[_vote("ORB", Direction.CE, 0.6), _vote("OI_BUILDUP", Direction.PE, 0.55)],
        shadow_direction=Direction.CE,
        shadow_score=0.5,
        ml_ce_prob=0.52,
    )
    assert result.vetoed
    assert result.direction is None


def test_consensus_picks_clear_ce_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "0.5")
    monkeypatch.setenv("DIRECTION_CONSENSUS_ML_WEIGHT", "0.2")
    snap = _snap()
    result = resolve_direction_consensus(
        snap=snap,
        rule_votes=[
            _vote("ORB", Direction.CE, 0.9),
            _vote("VWAP_RECLAIM", Direction.CE, 0.85),
        ],
        shadow_direction=Direction.CE,
        shadow_score=2.0,
        ml_ce_prob=0.55,
    )
    assert not result.vetoed
    assert result.direction == Direction.CE


# ── ML-confidence gate (Phase 2: trade only when direction-ML is confident) ──

def _clear_ce_votes() -> list[StrategyVote]:
    return [_vote("ORB", Direction.CE, 0.9), _vote("VWAP_RECLAIM", Direction.CE, 0.85)]


def test_ml_confidence_gate_vetoes_low_conf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "0.5")
    monkeypatch.setenv("DIRECTION_ML_CONFIDENCE_MIN", "0.60")
    result = resolve_direction_consensus(
        snap=_snap(), rule_votes=_clear_ce_votes(),
        shadow_direction=Direction.CE, shadow_score=2.0,
        ml_ce_prob=0.52,  # chosen side CE -> ML prob 0.52 < 0.60 gate
    )
    assert result.vetoed
    assert result.direction is None
    assert "ml_confidence" in result.veto_reason


def test_ml_confidence_gate_passes_high_conf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "0.5")
    monkeypatch.setenv("DIRECTION_ML_CONFIDENCE_MIN", "0.60")
    result = resolve_direction_consensus(
        snap=_snap(), rule_votes=_clear_ce_votes(),
        shadow_direction=Direction.CE, shadow_score=2.0,
        ml_ce_prob=0.72,  # CE prob 0.72 >= 0.60 gate -> passes
    )
    assert not result.vetoed
    assert result.direction == Direction.CE
    assert result.sources.get("ml_chosen_prob") == pytest.approx(0.72)


def test_ml_confidence_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "0.5")
    monkeypatch.delenv("DIRECTION_ML_CONFIDENCE_MIN", raising=False)  # default 0 = off
    result = resolve_direction_consensus(
        snap=_snap(), rule_votes=_clear_ce_votes(),
        shadow_direction=Direction.CE, shadow_score=2.0,
        ml_ce_prob=0.52,  # low conf, but gate disabled -> still trades
    )
    assert not result.vetoed
    assert result.direction == Direction.CE


def test_ml_confidence_gate_uses_pe_side_prob(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chosen side PE: gate must check (1 - ml_ce_prob). ml_ce_prob=0.55 -> PE prob 0.45 < 0.60 -> veto.
    monkeypatch.setenv("DIRECTION_CONSENSUS_MIN_MARGIN", "0.5")
    monkeypatch.setenv("DIRECTION_ML_CONFIDENCE_MIN", "0.60")
    snap = _snap(futures_bar={"fut_close": 50000.0, "fut_return_5m": -0.001})
    result = resolve_direction_consensus(
        snap=snap,
        rule_votes=[_vote("ORB", Direction.PE, 0.9), _vote("VWAP_RECLAIM", Direction.PE, 0.85)],
        shadow_direction=Direction.PE, shadow_score=2.0,
        ml_ce_prob=0.55,
    )
    assert result.vetoed
    assert "ml_confidence" in result.veto_reason
