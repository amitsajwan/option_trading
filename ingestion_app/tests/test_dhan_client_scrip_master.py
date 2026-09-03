"""Regression test for the 2026-09-03 NIFTY futures-contract mismatch incident.

ScripMaster.find_nearest_futures("NIFTY") matched "NIFTYFPI SEP FUT" (a different,
unrelated futures contract) instead of the real "NIFTY SEP FUT" 50-index futures,
because the old `name.startswith(underlying)` check has no word boundary. Since
NIFTYFPI shared NIFTY's exact nearest expiry date and sorted first, the wrong
contract's price (~1561 instead of NIFTY's real ~23800+) silently fed NIFTY's
ATM-strike calculation, leaving the seller's option chain with zero strikes below
ATM for weeks (real money, strategy_app/seller/brain.py -- iron_condor/bull_put
could never be built, only bear_call).
"""
from __future__ import annotations

from ingestion_app.dhan_client import ScripMaster


def _row(symbol, expiry, security_id):
    return {
        "SEM_CUSTOM_SYMBOL": symbol,
        "SEM_INSTRUMENT_NAME": "FUTIDX",
        "SEM_EXPIRY_DATE": expiry,
        "SEM_SMST_SECURITY_ID": security_id,
    }


# Real rows pulled from the live Dhan scrip master, 2026-09-03.
ROWS = [
    _row("NIFTYFPI SEP FUT", "2026-09-29 14:30:00", "35938"),
    _row("NIFTY SEP FUT", "2026-09-29 14:30:00", "68407"),
    _row("NIFTYNXT50 SEP FUT", "2026-09-29 14:30:00", "68412"),
    _row("BANKNIFTY SEP FUT", "2026-09-29 14:30:00", "68390"),
    _row("NIFTYFPI OCT FUT", "2026-10-27 14:30:00", "35956"),
]


def test_nifty_does_not_match_niftyfpi_or_niftynxt50():
    sm = ScripMaster(ROWS)
    row = sm.find_nearest_futures("NIFTY")
    assert row is not None
    assert row["SEM_SMST_SECURITY_ID"] == "68407"
    assert row["SEM_CUSTOM_SYMBOL"] == "NIFTY SEP FUT"


def test_banknifty_still_matches_correctly():
    sm = ScripMaster(ROWS)
    row = sm.find_nearest_futures("BANKNIFTY")
    assert row is not None
    assert row["SEM_SMST_SECURITY_ID"] == "68390"


def test_hyphenated_trading_symbol_format_also_respects_word_boundary():
    """SEM_TRADING_SYMBOL uses a different format (hyphens, no SEM_CUSTOM_SYMBOL)
    -- must be equally protected against the same prefix-collision bug."""
    rows = [
        {"SEM_TRADING_SYMBOL": "NIFTYFPI-Sep2026-FUT", "SEM_INSTRUMENT_NAME": "FUTIDX",
         "SEM_EXPIRY_DATE": "2026-09-29 14:30:00", "SEM_SMST_SECURITY_ID": "35938"},
        {"SEM_TRADING_SYMBOL": "NIFTY-Sep2026-FUT", "SEM_INSTRUMENT_NAME": "FUTIDX",
         "SEM_EXPIRY_DATE": "2026-09-29 14:30:00", "SEM_SMST_SECURITY_ID": "68407"},
    ]
    sm = ScripMaster(rows)
    row = sm.find_nearest_futures("NIFTY")
    assert row["SEM_SMST_SECURITY_ID"] == "68407"
