"""Unit tests for EntryConfig.

Covers: every knob parsed from env, bad-value rejection, defaults, assert_consistency,
time_window_allows, and regime_tag_allows.
"""
from __future__ import annotations

import pytest

from strategy_app.engines.entry_config import EntryConfig
from strategy_app.constants import MIN_ENTRY_CONFIDENCE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_ENV: dict[str, str] = {}


def make(env: dict[str, str] | None = None) -> EntryConfig:
    return EntryConfig.from_env(env or {})


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

def test_defaults_produce_valid_config():
    cfg = make()
    assert 0.0 <= cfg.min_confidence <= 1.0
    assert 0.0 <= cfg.bypass_min_confidence <= 1.0
    assert 0.0 <= cfg.regime_min_confidence <= 1.0
    assert cfg.max_premium >= 0
    assert cfg.max_session_trades >= 1


def test_default_min_confidence():
    cfg = make()
    assert cfg.min_confidence == MIN_ENTRY_CONFIDENCE


def test_default_entry_time_windows_empty():
    cfg = make()
    assert cfg.entry_time_windows == ()


def test_default_regime_allowed_tags_empty():
    cfg = make()
    assert cfg.regime_allowed_tags == frozenset()


def test_default_hard_premium_cap_on():
    cfg = make()
    assert cfg.hard_premium_cap is True


# ---------------------------------------------------------------------------
# Confidence knobs
# ---------------------------------------------------------------------------

def test_min_confidence_parsed():
    cfg = make({"STRATEGY_MIN_CONFIDENCE": "0.70"})
    assert cfg.min_confidence == pytest.approx(0.70)


def test_bypass_min_confidence_parsed():
    cfg = make({"CONSENSUS_BYPASS_MIN_CONFIDENCE": "0.80"})
    assert cfg.bypass_min_confidence == pytest.approx(0.80)


def test_bypass_min_defaults_to_min_conf():
    cfg = make({"STRATEGY_MIN_CONFIDENCE": "0.72"})
    assert cfg.bypass_min_confidence == pytest.approx(0.72)


def test_regime_min_confidence_parsed():
    cfg = make({"STRATEGY_REGIME_MIN_CONFIDENCE": "0.55"})
    assert cfg.regime_min_confidence == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------

def test_time_windows_parsed():
    cfg = make({"ENTRY_TIME_WINDOWS": "9:25-10:00,14:00-15:00"})
    assert (9 * 60 + 25, 10 * 60) in cfg.entry_time_windows
    assert (14 * 60, 15 * 60) in cfg.entry_time_windows


def test_time_windows_empty_string_gives_empty_tuple():
    cfg = make({"ENTRY_TIME_WINDOWS": ""})
    assert cfg.entry_time_windows == ()


def test_time_window_allows_no_restriction():
    cfg = make()
    assert cfg.time_window_allows(9 * 60 + 15) is True
    assert cfg.time_window_allows(15 * 60) is True


def test_time_window_allows_inside_window():
    cfg = make({"ENTRY_TIME_WINDOWS": "9:25-10:00"})
    assert cfg.time_window_allows(9 * 60 + 30) is True


def test_time_window_blocks_outside_window():
    cfg = make({"ENTRY_TIME_WINDOWS": "9:25-10:00"})
    assert cfg.time_window_allows(10 * 60 + 1) is False


def test_time_window_boundary_start_included():
    cfg = make({"ENTRY_TIME_WINDOWS": "9:25-10:00"})
    assert cfg.time_window_allows(9 * 60 + 25) is True


def test_time_window_boundary_end_excluded():
    cfg = make({"ENTRY_TIME_WINDOWS": "9:25-10:00"})
    assert cfg.time_window_allows(10 * 60) is False


def test_time_window_malformed_ignored():
    cfg = make({"ENTRY_TIME_WINDOWS": "bogus,9:30-10:00"})
    assert len(cfg.entry_time_windows) == 1


# ---------------------------------------------------------------------------
# Regime tags
# ---------------------------------------------------------------------------

def test_regime_allowed_tags_parsed():
    cfg = make({"ENTRY_REGIME_ALLOWED_TAGS": "bear,chop"})
    assert "bear" in cfg.regime_allowed_tags
    assert "chop" in cfg.regime_allowed_tags
    assert "bull" not in cfg.regime_allowed_tags


def test_regime_tag_allows_no_restriction():
    cfg = make()
    assert cfg.regime_tag_allows("bear") is True
    assert cfg.regime_tag_allows("bull") is True
    assert cfg.regime_tag_allows(None) is True


def test_regime_tag_allows_matching_tag():
    cfg = make({"ENTRY_REGIME_ALLOWED_TAGS": "bear,chop"})
    assert cfg.regime_tag_allows("bear") is True
    assert cfg.regime_tag_allows("chop") is True


def test_regime_tag_blocks_non_matching():
    cfg = make({"ENTRY_REGIME_ALLOWED_TAGS": "bear,chop"})
    assert cfg.regime_tag_allows("bull") is False


def test_regime_tag_blocks_none_when_restricted():
    cfg = make({"ENTRY_REGIME_ALLOWED_TAGS": "bear"})
    assert cfg.regime_tag_allows(None) is False
    assert cfg.regime_tag_allows("unknown") is False


def test_regime_tag_case_insensitive():
    cfg = make({"ENTRY_REGIME_ALLOWED_TAGS": "BEAR,CHOP"})
    assert cfg.regime_tag_allows("bear") is True


def test_regime_tagger_parsed():
    cfg = make({"ENTRY_REGIME_TAGGER": "gap_03pct"})
    assert cfg.regime_tagger == "gap_03pct"


# ---------------------------------------------------------------------------
# Direction knobs
# ---------------------------------------------------------------------------

def test_direction_ml_weight_parsed():
    cfg = make({"DIRECTION_CONSENSUS_ML_WEIGHT": "0.5"})
    assert cfg.ml_direction_weight == pytest.approx(0.5)


def test_ml_block_pe_parsed():
    cfg = make({"ML_ENTRY_BLOCK_PE": "1"})
    assert cfg.ml_block_pe is True


def test_ml_block_ce_parsed():
    cfg = make({"ML_ENTRY_BLOCK_CE": "true"})
    assert cfg.ml_block_ce is True


def test_ml_block_defaults_false():
    cfg = make()
    assert cfg.ml_block_pe is False
    assert cfg.ml_block_ce is False


# ---------------------------------------------------------------------------
# Strike / depth
# ---------------------------------------------------------------------------

def test_strike_policy_parsed():
    cfg = make({"STRATEGY_STRIKE_SELECTION_POLICY": "smart_strike"})
    assert cfg.strike_policy == "smart_strike"


def test_strike_policy_lowercased():
    cfg = make({"STRATEGY_STRIKE_SELECTION_POLICY": "ATM"})
    assert cfg.strike_policy == "atm"


def test_max_premium_parsed():
    cfg = make({"SMART_STRIKE_MAX_PREMIUM": "500"})
    assert cfg.max_premium == pytest.approx(500.0)


def test_hard_premium_cap_false():
    cfg = make({"SMART_STRIKE_HARD_PREMIUM_CAP": "0"})
    assert cfg.hard_premium_cap is False


def test_max_otm_steps_parsed():
    cfg = make({"STRATEGY_STRIKE_MAX_OTM_STEPS": "8"})
    assert cfg.max_otm_steps == 8


def test_iv_reject_pctile_parsed():
    cfg = make({"SMART_STRIKE_IV_REJECT_PCTILE": "0.90"})
    assert cfg.iv_reject_pctile == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# Risk knobs
# ---------------------------------------------------------------------------

def test_max_session_trades_parsed():
    cfg = make({"RISK_MAX_SESSION_TRADES": "20"})
    assert cfg.max_session_trades == 20


def test_max_consecutive_losses_parsed():
    cfg = make({"RISK_MAX_CONSECUTIVE_LOSSES": "5"})
    assert cfg.max_consecutive_losses == 5


def test_max_lots_per_trade_parsed():
    cfg = make({"RISK_MAX_LOTS_PER_TRADE": "3"})
    assert cfg.max_lots_per_trade == 3


# ---------------------------------------------------------------------------
# assert_consistency — valid config
# ---------------------------------------------------------------------------

def test_assert_consistency_valid_passes(caplog):
    import logging
    cfg = make()
    with caplog.at_level(logging.INFO, logger="strategy_app.engines.entry_config"):
        cfg.assert_consistency()
    assert "entry_config_effective" in caplog.text


# ---------------------------------------------------------------------------
# assert_consistency — invalid values raise
# ---------------------------------------------------------------------------

def test_assert_consistency_min_confidence_out_of_range():
    cfg = make({"STRATEGY_MIN_CONFIDENCE": "1.5"})
    with pytest.raises(ValueError, match="min_confidence"):
        cfg.assert_consistency()


def test_assert_consistency_bypass_confidence_out_of_range():
    cfg = make({
        "STRATEGY_MIN_CONFIDENCE": "0.65",
        "CONSENSUS_BYPASS_MIN_CONFIDENCE": "-0.1",
    })
    with pytest.raises(ValueError, match="bypass_min_confidence"):
        cfg.assert_consistency()


def test_assert_consistency_negative_premium():
    cfg = make({"SMART_STRIKE_MAX_PREMIUM": "-10"})
    with pytest.raises(ValueError, match="max_premium"):
        cfg.assert_consistency()


def test_assert_consistency_zero_session_trades():
    cfg = make({"RISK_MAX_SESSION_TRADES": "0"})
    with pytest.raises(ValueError, match="max_session_trades"):
        cfg.assert_consistency()


# ---------------------------------------------------------------------------
# Bad env values raise on from_env
# ---------------------------------------------------------------------------

def test_bad_float_raises():
    with pytest.raises(ValueError, match="STRATEGY_MIN_CONFIDENCE"):
        make({"STRATEGY_MIN_CONFIDENCE": "not_a_float"})


def test_bad_int_raises():
    with pytest.raises(ValueError, match="RISK_MAX_SESSION_TRADES"):
        make({"RISK_MAX_SESSION_TRADES": "abc"})


# ---------------------------------------------------------------------------
# EntryConfig is frozen
# ---------------------------------------------------------------------------

def test_entry_config_is_frozen():
    cfg = make()
    with pytest.raises((AttributeError, TypeError)):
        cfg.min_confidence = 0.99  # type: ignore[misc]
