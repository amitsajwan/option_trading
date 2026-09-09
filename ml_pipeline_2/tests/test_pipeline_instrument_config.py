from __future__ import annotations

import pytest

from ml_pipeline_2.pipeline.instrument_config import (
    INSTRUMENT_ML_CONFIGS,
    get_instrument_config,
    mongo_hist_collection_for,
)


def test_all_five_current_instruments_registered() -> None:
    assert set(INSTRUMENT_ML_CONFIGS) == {"BANKNIFTY", "NIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"}


def test_lookup_is_case_insensitive() -> None:
    assert get_instrument_config("nifty").lot_size == 65
    assert get_instrument_config("NIFTY").lot_size == 65


def test_unknown_instrument_fails_loud() -> None:
    with pytest.raises(KeyError, match="no ML pipeline config registered"):
        get_instrument_config("DOWJONES")


def test_verified_lot_sizes() -> None:
    # These are the exact values confirmed against the live .env.compose
    # / compose-file defaults on 2026-09-09 -- a wrong value here silently
    # scales every rupee P&L figure a backtest reports.
    assert get_instrument_config("BANKNIFTY").lot_size == 30
    assert get_instrument_config("NIFTY").lot_size == 65
    assert get_instrument_config("FINNIFTY").lot_size == 60
    assert get_instrument_config("SENSEX").lot_size == 20
    assert get_instrument_config("MIDCPNIFTY").lot_size == 120


def test_banknifty_uses_bare_collection_name() -> None:
    # BankNifty is the "primary" instrument -- no _banknifty suffix.
    assert mongo_hist_collection_for("BANKNIFTY") == "phase1_market_snapshots_hist"


def test_other_instruments_use_suffixed_collection_name() -> None:
    assert mongo_hist_collection_for("NIFTY") == "phase1_market_snapshots_hist_nifty"
    assert mongo_hist_collection_for("SENSEX") == "phase1_market_snapshots_hist_sensex"


def test_midcpnifty_has_no_live_model_yet() -> None:
    cfg = get_instrument_config("MIDCPNIFTY")
    assert cfg.live_model_path is None
    assert cfg.live_entry_min_prob is None
