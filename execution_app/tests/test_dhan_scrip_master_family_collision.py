"""_ScripMaster's underlying filter must be an exact family match, not substring.

Found 2026-07-23 via a live lot-size drift warning on the NIFTY execution
container (scrip_master=60 registry=65). Root cause: the old filter was
`_UNDERLYING in trading_symbol.upper()`. "NIFTY" is a substring of
BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50's trading symbols too, so a NIFTY
container's scrip index silently absorbed all five families. All five share
overlapping (expiry, strike, option_type) ranges (122 real collisions with
FINNIFTY alone in NIFTY's near-the-money 23000-26000 zone for one real
expiry, confirmed against Dhan's live scrip master) and the index is a plain
dict keyed on (expiry, strike, option_type) -- underlying is NOT part of the
key -- so whichever family's row the CSV iterated last silently overwrote
the correct entry. This could resolve the wrong security_id for a real
order: a "NIFTY" signal could place against FINNIFTY's contract instead.
"""
from __future__ import annotations

import io
import os
import tempfile
from datetime import date, datetime, timezone

import execution_app.adapter.dhan as dhan_mod

_HEADER = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
    "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
    "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME"
)


def _row(sec_id, sym, lot, expiry, strike, opt):
    return (
        f"NSE,D,{sec_id},OPTIDX,0,{sym},{lot},{sym},"
        f"{expiry} 14:30:00,{strike}.00000,{opt},0.05,0,OPTIDX,,{sym.split('-')[0]}"
    )


def _fake_csv() -> str:
    # Same (expiry, strike, option_type) offered by both NIFTY and FINNIFTY --
    # the exact real-world collision shape found 2026-07-23.
    rows = [
        _row("111", "NIFTY-Jul2026-24000-CE", "65", "2026-07-28", "24000", "CE"),
        _row("222", "FINNIFTY-Jul2026-24000-CE", "60", "2026-07-28", "24000", "CE"),
        _row("333", "BANKNIFTY-Jul2026-24000-CE", "30", "2026-07-28", "24000", "CE"),
        _row("444", "MIDCPNIFTY-Jul2026-24000-CE", "120", "2026-07-28", "24000", "CE"),
        _row("555", "NIFTYNXT50-Jul2026-24000-CE", "25", "2026-07-28", "24000", "CE"),
    ]
    return "\n".join([_HEADER] + rows) + "\n"


def _scrip_master_with_cache(tmpdir: str):
    cache_path = os.path.join(tmpdir, "scrip_master.csv")
    with open(cache_path, "w", encoding="utf-8") as fh:
        fh.write(_fake_csv())
    return dhan_mod._ScripMaster(cache_path=cache_path)


def test_nifty_underlying_does_not_absorb_finnifty_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = _scrip_master_with_cache(tmpdir)
        prior = dhan_mod._UNDERLYING
        dhan_mod._UNDERLYING = "NIFTY"
        try:
            resolved = sm.resolve(date(2026, 7, 28), 24000, "CE")
        finally:
            dhan_mod._UNDERLYING = prior
        assert resolved == ("111", 65), (
            f"expected NIFTY's own row (sec_id=111, lot=65), got {resolved} -- "
            "a family-collision row silently won the index"
        )


def test_finnifty_underlying_does_not_absorb_nifty_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = _scrip_master_with_cache(tmpdir)
        prior = dhan_mod._UNDERLYING
        dhan_mod._UNDERLYING = "FINNIFTY"
        try:
            resolved = sm.resolve(date(2026, 7, 28), 24000, "CE")
        finally:
            dhan_mod._UNDERLYING = prior
        assert resolved == ("222", 60)


def test_banknifty_underlying_does_not_absorb_nifty_row():
    """Regression guard: BANKNIFTY wasn't actually vulnerable to the substring
    bug (it's not a substring of the other four), but pin the behavior anyway."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = _scrip_master_with_cache(tmpdir)
        prior = dhan_mod._UNDERLYING
        dhan_mod._UNDERLYING = "BANKNIFTY"
        try:
            resolved = sm.resolve(date(2026, 7, 28), 24000, "CE")
        finally:
            dhan_mod._UNDERLYING = prior
        assert resolved == ("333", 30)
