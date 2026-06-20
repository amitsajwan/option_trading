"""Tests for entry-quality grading + live/paper tiering.

The headline case is the 2026-06-03 fce59da2 trade: a CE chosen on a SIDEWAYS
day, direction margin manufactured by correlated depth, iv_skew leaning PE,
momentum already reversing. It must grade BAD and route to paper, never live.
"""
from __future__ import annotations

import pytest

from strategy_app.contracts import Direction, RiskContext
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.entry_direction_resolver import EntryDirectionResult
from strategy_app.signals.entry_quality import (
    BAD,
    GOOD,
    OK,
    decide_tier,
    grade_entry,
    grade_entry_from_raw,
)


def _snap(**fd: object) -> SnapshotAccessor:
    base = {
        "fut_return_1m": 0.0001,
        "fut_return_5m": 0.0015,
        "atm_ce_iv": 60.0,
        "atm_pe_iv": 60.0,
    }
    base.update(fd)
    return SnapshotAccessor({
        "snapshot_id": "s1",
        "timestamp": "2026-06-03T08:54:00+00:00",
        "trade_date": "2026-06-03",
        "futures_derived": {k: v for k, v in base.items() if k.startswith("fut_")},
        "strikes": [],
        # SnapshotAccessor.atm_*_iv falls back to top-level atm_ce_iv/atm_pe_iv
        "atm_ce_iv": base["atm_ce_iv"],
        "atm_pe_iv": base["atm_pe_iv"],
    })


def _clean_dir(direction=Direction.CE, margin=3.0) -> EntryDirectionResult:
    ce = margin if direction == Direction.CE else 0.0
    pe = margin if direction == Direction.PE else 0.0
    return EntryDirectionResult(
        direction=direction, source="composite(momentum_5m:CE,vwap:CE)",
        ce_score=ce, pe_score=pe, margin=margin,
        sources={"momentum_5m:CE": 1.0, "vwap:CE": 1.0, "orb_low_reject:CE": 1.0},
    )


def test_clean_trend_entry_grades_good() -> None:
    res = grade_entry(_clean_dir(), _snap(), regime="TRENDING")
    assert res.grade == GOOD
    assert res.score >= 0.8


def test_vetoed_direction_is_bad() -> None:
    vetoed = EntryDirectionResult(direction=None, source="composite_veto",
                                  vetoed=True, veto_reason="low_margin")
    res = grade_entry(vetoed, _snap(), regime="TRENDING")
    assert res.grade == BAD


def test_sideways_chop_downgrades() -> None:
    trend = grade_entry(_clean_dir(), _snap(), regime="TRENDING")
    chop = grade_entry(_clean_dir(), _snap(), regime="SIDEWAYS")
    assert chop.score < trend.score
    assert any("chop_regime" in r for r in chop.reasons)


def test_stale_momentum_penalised() -> None:
    # r1m negative while r5m positive -> the move is reversing.
    res = grade_entry(_clean_dir(), _snap(fut_return_1m=-0.0003, fut_return_5m=0.0015),
                      regime="TRENDING")
    assert any("stale_momentum" in r for r in res.reasons)


def test_fce59da2_shaped_entry_is_bad_and_paper() -> None:
    """SIDEWAYS + depth-dominated margin + iv_skew->PE + stale momentum -> BAD."""
    dir_result = EntryDirectionResult(
        direction=Direction.CE,
        source="composite(depth_ce:bid_dom->CE,depth_ce:micro+->CE)",
        ce_score=1.5, pe_score=0.4, margin=1.1,
        # depth_net carries most of the winning-side score -> depth-dominant
        sources={"depth_net": 1.1, "iv_skew:PE": 0.5},
    )
    snap = _snap(
        fut_return_1m=-0.00026,  # reversing
        fut_return_5m=0.00147,
        # ABNORMAL put skew (1.40x) — beyond the normal index 1.2-1.3 band, so it
        # is a genuine downside lean that disagrees with this CE.
        atm_ce_iv=50.0, atm_pe_iv=70.0,
    )
    res = grade_entry(dir_result, snap, regime="SIDEWAYS")
    assert res.grade == BAD, res.reasons
    assert any("chop_regime" in r for r in res.reasons)
    assert any("depth_dominant" in r for r in res.reasons)

    # And it must route to paper.
    tier = decide_tier(res.grade, RiskContext(), confidence=0.65)
    assert tier.tier == "paper"
    assert tier.live_would_take is False


def test_normal_index_put_skew_does_not_penalise_ce() -> None:
    """Routine index put skew (PE IV ~1.25-1.32x CE IV) must NOT fire iv_skew on a
    CE — it is structural, not a directional signal (data-confirmed 2026-06-02)."""
    dir_result = _clean_dir(direction=Direction.CE, margin=3.0)
    snap = _snap(atm_ce_iv=50.0, atm_pe_iv=63.0)  # ratio 1.26 = normal
    res = grade_entry(dir_result, snap, regime="TRENDING")
    assert not any("iv_skew_disagree" in r for r in res.reasons), res.reasons
    assert res.grade == GOOD
    # A clean GOOD trade in a clean session is live-eligible.
    tier = decide_tier(res.grade, RiskContext(), confidence=0.9)
    assert tier.tier == "live"


def test_grade_from_raw_returns_none_without_scores() -> None:
    assert grade_entry_from_raw({"direction_source": "ce_only"}, _snap(),
                                direction=Direction.CE, regime="TRENDING") is None


def test_grade_from_raw_reconstructs_and_grades() -> None:
    raw = {
        "direction_source": "composite",
        "entry_dir_ce_score": 3.0,
        "entry_dir_pe_score": 0.0,
        "entry_dir_margin": 3.0,
        "entry_dir_sources": {"momentum_5m:CE": 1.0, "vwap:CE": 1.0},
    }
    res = grade_entry_from_raw(raw, _snap(), direction=Direction.CE, regime="TRENDING")
    assert res is not None
    assert res.grade in (GOOD, OK)


# ── Tier / live-eligibility ─────────────────────────────────────────────────

def test_good_grade_clean_session_goes_live() -> None:
    tier = decide_tier(GOOD, RiskContext(), confidence=0.9)
    assert tier.tier == "live"
    assert tier.live_would_take is True


def test_ok_grade_paper_by_default_min_grade_good(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_LIVE_MIN_GRADE", raising=False)
    tier = decide_tier(OK, RiskContext(), confidence=0.9)
    assert tier.tier == "paper"
    assert "grade_below_min" in tier.reason


def test_ok_grade_can_go_live_when_min_grade_relaxed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_LIVE_MIN_GRADE", "OK")
    tier = decide_tier(OK, RiskContext(), confidence=0.9)
    assert tier.tier == "live"


def test_consecutive_loss_caution_forces_paper() -> None:
    risk = RiskContext(consecutive_losses=2, max_consecutive_losses=3)
    tier = decide_tier(GOOD, risk, confidence=0.9)
    assert tier.tier == "paper"
    assert "consec_loss_caution" in tier.reason


def test_defensive_states_force_paper() -> None:
    for kw in ("daily_loss_breached", "weekly_loss_breached",
               "session_trade_cap_breached", "vix_spike_halt", "consecutive_loss_limit"):
        risk = RiskContext(**{kw: True})
        tier = decide_tier(GOOD, risk, confidence=0.9)
        assert tier.tier == "paper", kw


def test_session_winrate_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_LIVE_MIN_WINRATE", "0.5")
    monkeypatch.setenv("RISK_LIVE_WINRATE_MIN_TRADES", "4")
    # 1 win / 4 losses = 20% over 5 decided trades -> below 50% floor.
    risk = RiskContext(session_win_count=1, session_loss_count=4, max_consecutive_losses=0)
    tier = decide_tier(GOOD, risk, confidence=0.9)
    assert tier.tier == "paper"
    assert "session_winrate_low" in tier.reason


def test_winrate_floor_ignored_below_min_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_LIVE_MIN_WINRATE", "0.5")
    monkeypatch.setenv("RISK_LIVE_WINRATE_MIN_TRADES", "10")
    # Only 2 decided trades -> floor not applied yet.
    risk = RiskContext(session_win_count=0, session_loss_count=2, max_consecutive_losses=0)
    tier = decide_tier(GOOD, risk, confidence=0.9)
    assert tier.tier == "live"
