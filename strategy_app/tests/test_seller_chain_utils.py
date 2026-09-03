"""Regression test for the 2026-09-03 FINNIFTY false take_profit_50 incident.

A frozen/stale feed left an illiquid strike quoting an LTP that violated basic
OTM-strike monotonicity (further-OTM priced richer than closer-to-money), which
fed a bogus spread value into the take-profit check and fired 57 phantom trades
in one day. build_price_fn must not trust an "exact hit" strike with no real
trading activity (zero volume and no open interest) — it should fall through to
the same far-OTM-proxy / nearest-liquid-neighbor fallback already used for
missing strikes.
"""
from __future__ import annotations

from strategy_app.seller.chain_utils import build_price_fn, OTM_PROXY_PRICE


def _snap(strikes, atm):
    return {"strikes": strikes, "chain_aggregates": {"atm_strike": atm}}


def _row(strike, ce_ltp=None, pe_ltp=None, ce_volume=0, ce_oi=None, pe_volume=0, pe_oi=None):
    return {
        "strike": strike,
        "ce_ltp": ce_ltp, "ce_volume": ce_volume, "ce_oi": ce_oi,
        "pe_ltp": pe_ltp, "pe_volume": pe_volume, "pe_oi": pe_oi,
    }


def test_liquid_exact_hit_is_trusted():
    """A strike with real volume/OI resolves to its own LTP, as before."""
    snap = _snap([_row(26500, pe_ltp=341.25, pe_volume=16680, pe_oi=16560)], atm=26600)
    pf = build_price_fn(snap)
    assert pf("PE", 26500) == 341.25


def test_illiquid_exact_hit_falls_back_instead_of_trusted():
    """Reproduces the incident: a far-OTM strike (26350, 250pt OTM) quoting a
    stale LTP richer than a closer, liquid strike (26500, 100pt OTM) must NOT
    be trusted verbatim -- it should fall through to the liquid neighbor."""
    snap = _snap(
        [
            _row(26350, pe_ltp=640.60, pe_volume=0, pe_oi=None),   # stale, illiquid
            _row(26500, pe_ltp=341.25, pe_volume=16680, pe_oi=16560),  # real, liquid
        ],
        atm=26600,
    )
    pf = build_price_fn(snap)
    # Must NOT return the impossible stale price (640.60, richer than a strike
    # closer to the money -- the exact defect that produced the false TP).
    price = pf("PE", 26350)
    assert price != 640.60
    # Falls back to the nearest liquid neighbor within range.
    assert price == 341.25


def test_illiquid_strike_with_no_liquid_neighbor_uses_far_otm_proxy():
    snap = _snap([_row(26350, pe_ltp=640.60, pe_volume=0, pe_oi=None)], atm=27100)
    pf = build_price_fn(snap)
    # 750pt OTM, beyond OTM_PROXY_THRESHOLD, and no liquid neighbor at all.
    assert pf("PE", 26350) == OTM_PROXY_PRICE


def test_illiquid_strike_with_zero_oi_field_present_still_rejected():
    """oi=0 (not just null) must also count as no open interest."""
    snap = _snap(
        [
            _row(26350, pe_ltp=640.60, pe_volume=0, pe_oi=0),
            _row(26500, pe_ltp=341.25, pe_volume=16680, pe_oi=16560),
        ],
        atm=26600,
    )
    pf = build_price_fn(snap)
    assert pf("PE", 26350) == 341.25


def test_volume_alone_is_sufficient_liquidity_signal():
    """Some feeds report volume but not OI -- either one backing the quote is enough."""
    snap = _snap([_row(26500, pe_ltp=341.25, pe_volume=100, pe_oi=None)], atm=26600)
    pf = build_price_fn(snap)
    assert pf("PE", 26500) == 341.25
