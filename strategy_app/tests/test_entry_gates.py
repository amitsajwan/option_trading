"""Tests for entry_gates: time-window + daily-regime filters."""
from __future__ import annotations

import os

import pytest

from strategy_app.engines.entry_gates import (
    _parse_windows,
    compute_regime_tag,
    is_in_configured_time_window,
    is_session_regime_allowed,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _snap(time_ist: str = "10:00:00",
          fut_open: float | None = 48000.0,
          prev_close: float | None = 48000.0,
          overnight_gap: float | None = 0.0,
          pcr: float | None = 1.0,
          orh_broken: bool = False,
          orl_broken: bool = False) -> SnapshotAccessor:
    payload = {
        "snapshot_id": "s",
        "timestamp": f"2024-08-15T{time_ist}+05:30",
        "trade_date": "2024-08-15",
        "session_context": {
            "snapshot_id": "s",
            "timestamp": f"2024-08-15T{time_ist}+05:30",
            "date": "2024-08-15",
            "time": time_ist,
            "session_phase": "ACTIVE",
        },
        "futures_bar": {"fut_open": fut_open} if fut_open is not None else {},
        "session_levels": {
            "prev_day_close": prev_close,
            "overnight_gap": overnight_gap,
            "prev_day_pcr": pcr,
        },
        "opening_range": {"orh_broken": orh_broken, "orl_broken": orl_broken},
    }
    return SnapshotAccessor(payload)


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------
def test_parse_windows_basic() -> None:
    assert _parse_windows("09:45-10:15,10:45-11:15") == [
        (9 * 60 + 45, 10 * 60 + 15),
        (10 * 60 + 45, 11 * 60 + 15),
    ]


def test_parse_windows_handles_garbage() -> None:
    assert _parse_windows("") == []
    assert _parse_windows("bad") == []
    assert _parse_windows("09:45-09:45") == []  # zero-width window dropped
    assert _parse_windows("09:45-10:00, bad, 10:30-10:45") == [
        (9 * 60 + 45, 10 * 60),
        (10 * 60 + 30, 10 * 60 + 45),
    ]


# ---------------------------------------------------------------------------
# Time-window gate
# ---------------------------------------------------------------------------
def test_time_window_disabled_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("ENTRY_TIME_WINDOWS", raising=False)
    assert is_in_configured_time_window(_snap("12:00:00")) is True


def test_time_window_allows_inside(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_TIME_WINDOWS", "09:45-10:15,10:45-11:15")
    assert is_in_configured_time_window(_snap("09:46:00")) is True
    assert is_in_configured_time_window(_snap("10:14:59")) is True
    assert is_in_configured_time_window(_snap("11:00:00")) is True


def test_time_window_blocks_outside(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_TIME_WINDOWS", "09:45-10:15,10:45-11:15")
    assert is_in_configured_time_window(_snap("09:30:00")) is False
    assert is_in_configured_time_window(_snap("10:30:00")) is False
    assert is_in_configured_time_window(_snap("12:00:00")) is False


def test_time_window_blocks_when_timestamp_missing(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_TIME_WINDOWS", "09:45-10:15")
    payload = {"snapshot_id": "s", "session_context": {}}
    assert is_in_configured_time_window(SnapshotAccessor(payload)) is False


# ---------------------------------------------------------------------------
# Regime taggers
# ---------------------------------------------------------------------------
def test_gap_tagger_classifies_correctly() -> None:
    assert compute_regime_tag("gap_03pct", _snap(overnight_gap=0.005)) == "bull"
    assert compute_regime_tag("gap_03pct", _snap(overnight_gap=-0.005)) == "bear"
    assert compute_regime_tag("gap_03pct", _snap(overnight_gap=0.001)) == "chop"
    assert compute_regime_tag("gap_03pct", _snap(overnight_gap=None)) == "unknown"


def test_open_vs_prev_tagger() -> None:
    # +0.5% gap → bull
    snap = _snap(fut_open=48240.0, prev_close=48000.0)
    assert compute_regime_tag("open_vs_prev_02pct", snap) == "bull"
    # -0.5% → bear
    snap = _snap(fut_open=47760.0, prev_close=48000.0)
    assert compute_regime_tag("open_vs_prev_02pct", snap) == "bear"
    # within +/- 0.2% → chop
    snap = _snap(fut_open=48050.0, prev_close=48000.0)
    assert compute_regime_tag("open_vs_prev_02pct", snap) == "chop"


def test_orb_tagger_requires_break() -> None:
    assert compute_regime_tag("orb_at_945", _snap(orh_broken=False, orl_broken=False)) == "unknown"
    assert compute_regime_tag("orb_at_945", _snap(orh_broken=True, orl_broken=False)) == "bull"
    assert compute_regime_tag("orb_at_945", _snap(orh_broken=False, orl_broken=True)) == "bear"
    assert compute_regime_tag("orb_at_945", _snap(orh_broken=True, orl_broken=True)) == "chop"


def test_pcr_tagger() -> None:
    assert compute_regime_tag("pcr_prev_day", _snap(pcr=0.6)) == "bull"
    assert compute_regime_tag("pcr_prev_day", _snap(pcr=1.3)) == "bear"
    assert compute_regime_tag("pcr_prev_day", _snap(pcr=1.0)) == "chop"
    assert compute_regime_tag("pcr_prev_day", _snap(pcr=None)) == "unknown"


def test_combined_majority_bull_2of3() -> None:
    # gap=+0.3% (bull), open-vs-prev=+0.3% (bull), ORB broken up (bull) → bull (3-of-3)
    snap = _snap(
        overnight_gap=0.003,
        fut_open=48144.0,
        prev_close=48000.0,
        orh_broken=True,
    )
    assert compute_regime_tag("combined_majority", snap) == "bull"


def test_combined_majority_bear_2of3() -> None:
    snap = _snap(
        overnight_gap=-0.005,
        fut_open=47700.0,
        prev_close=48000.0,
        orl_broken=True,
    )
    assert compute_regime_tag("combined_majority", snap) == "bear"


def test_combined_majority_unknown_before_orb_resolves() -> None:
    # No ORB break yet — must wait
    snap = _snap(overnight_gap=0.01, fut_open=48500.0, prev_close=48000.0,
                 orh_broken=False, orl_broken=False)
    assert compute_regime_tag("combined_majority", snap) == "unknown"


def test_combined_majority_pcr_tiebreak() -> None:
    # One vote bull, one bear, one chop → no majority → PCR breaks tie
    snap = _snap(
        overnight_gap=0.005,        # bull
        fut_open=47700.0,           # open_vs_prev = -0.6% → bear
        prev_close=48000.0,
        orh_broken=True,            # OR broken (ORB up = bull)
        pcr=0.5,                    # bullish PCR
    )
    # Actually gap=bull, open_vs_prev=bear, orb=bull → bull majority (2-of-3), no tiebreak
    assert compute_regime_tag("combined_majority", snap) == "bull"
    # Force a real tie: gap=chop, open_vs_prev=bear, orb=bull → 1 bull, 1 bear, 1 chop
    snap = _snap(
        overnight_gap=0.0,
        fut_open=47700.0,
        prev_close=48000.0,
        orh_broken=True,
        pcr=1.3,                    # bearish PCR breaks tie
    )
    assert compute_regime_tag("combined_majority", snap) == "bear"


def test_unknown_tagger_returns_unknown() -> None:
    assert compute_regime_tag("does_not_exist", _snap()) == "unknown"
    assert compute_regime_tag("", _snap()) == "unknown"


# ---------------------------------------------------------------------------
# Regime-allowed gate
# ---------------------------------------------------------------------------
def test_regime_gate_disabled_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("ENTRY_REGIME_ALLOWED_TAGS", raising=False)
    assert is_session_regime_allowed("bull") is True
    assert is_session_regime_allowed(None) is True


def test_regime_gate_allows_matching_tag(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_REGIME_ALLOWED_TAGS", "bear,chop")
    assert is_session_regime_allowed("bear") is True
    assert is_session_regime_allowed("chop") is True


def test_regime_gate_blocks_non_matching(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_REGIME_ALLOWED_TAGS", "bear,chop")
    assert is_session_regime_allowed("bull") is False


def test_regime_gate_blocks_unknown_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_REGIME_ALLOWED_TAGS", "bear,chop")
    assert is_session_regime_allowed(None) is False
    assert is_session_regime_allowed("unknown") is False


def test_regime_gate_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_REGIME_ALLOWED_TAGS", "BEAR, CHOP ")
    assert is_session_regime_allowed("bear") is True
    assert is_session_regime_allowed("CHOP") is True
    assert is_session_regime_allowed("bull") is False
