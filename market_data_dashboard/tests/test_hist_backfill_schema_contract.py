"""Regression test for the 2026-08-11 finding: hist_backfill's snapshot
builder produced dicts missing 5 top-level blocks (mtf_derived, opening_range,
ladder_aggregates, iv_derived, session_levels) and dozens of required
sub-fields elsewhere -- nothing here ever ran the output through the same
market_snapshot_contract.validate_market_snapshot() the live and replay paths
already use, so the drift went unnoticed for a month and broke the isolated
month-long replay tool outright.

This builds a small synthetic Dhan-shaped raw payload, runs it through the
real build pipeline (build_snapshots_from_dhan_data -> _enrich_for_seller ->
enrich_day_mtf_opening_iv), and asserts zero contract errors. Values filled
only by the cross-day passes (iv_percentile_pass, session_levels_pass) are
legitimately None here -- the contract only requires the KEY to be present,
which this test enforces via the real validator, not a hand-rolled key list.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from market_data_dashboard.services.dhan_replay_builder import build_snapshots_from_dhan_data
from market_data_dashboard.services.hist_backfill import _enrich_for_seller, enrich_day_mtf_opening_iv
from snapshot_app.core.market_snapshot_contract import validate_market_snapshot

TRADE_DATE = "2026-07-15"


def _index_bars(n: int) -> list[dict]:
    base = datetime(2026, 7, 15, 9, 15)
    price = 50000.0
    bars = []
    for i in range(n):
        price += (i % 5) - 2
        bars.append({
            "ts": (base + timedelta(minutes=i)).isoformat(),
            "open": price, "high": price + 5, "low": price - 5, "close": price,
            "volume": 1000 + i,
        })
    return bars


def _option_bars(n: int, offset: int, atm_strike: int, step: int) -> dict:
    base = datetime(2026, 7, 15, 9, 15)
    strike = atm_strike + offset * step
    ce, pe = [], []
    for i in range(n):
        ts = (base + timedelta(minutes=i)).isoformat()
        ce.append({
            "ts": ts, "ce_close": 100.0 + offset, "ce_open": 99.0 + offset,
            "ce_high": 102.0 + offset, "ce_low": 98.0 + offset, "ce_iv": 0.15,
            "ce_oi": 1000 + i, "ce_volume": None, "spot": 50000.0, "strike": strike,
        })
        pe.append({
            "ts": ts, "pe_close": 90.0 - offset, "pe_open": 89.0 - offset,
            "pe_high": 92.0 - offset, "pe_low": 88.0 - offset, "pe_iv": 0.14,
            "pe_oi": 900 + i, "pe_volume": None, "spot": 50000.0, "strike": strike,
        })
    return {"ce": ce, "pe": pe}


def _build_day(n_bars: int = 40) -> list[dict]:
    atm_strike, step = 50000, 100
    options = {
        ("ATM" if off == 0 else (f"ATMp{off}" if off > 0 else f"ATMm{abs(off)}")):
            _option_bars(n_bars, off, atm_strike, step)
        for off in range(-3, 4)
    }
    raw = {
        "instrument": "BANKNIFTY",
        "trade_date": TRADE_DATE,
        "step": step,
        "atm_strike": atm_strike,
        "index_bars": _index_bars(n_bars),
        "vix_bars": [],
        "options": options,
    }
    snapshots = build_snapshots_from_dhan_data(raw)
    _enrich_for_seller(snapshots, TRADE_DATE, "BANKNIFTY")
    enrich_day_mtf_opening_iv(snapshots)
    return snapshots


class HistBackfillSchemaContractTest(unittest.TestCase):
    def test_backfilled_day_matches_live_schema_contract(self) -> None:
        snapshots = _build_day()
        self.assertGreater(len(snapshots), 0)

        all_errors: list[str] = []
        for s in snapshots:
            report = validate_market_snapshot(s, raise_on_error=False)
            all_errors.extend(report["errors"])

        self.assertEqual(
            all_errors, [],
            f"backfilled snapshots fail the live schema contract "
            f"({len(all_errors)} field errors) -- this is the exact drift "
            f"that broke month-long replay against phase1_market_snapshots_hist",
        )

    def test_five_previously_missing_blocks_are_present(self) -> None:
        """Narrower, explicit guard for the specific blocks that were 100%
        absent (not just individual sub-fields) before the 2026-08-11 fix."""
        snapshots = _build_day(n_bars=5)
        for block in ("mtf_derived", "opening_range", "ladder_aggregates", "iv_derived", "session_levels"):
            for s in snapshots:
                self.assertIn(block, s, f"{block} missing from backfilled snapshot")
                self.assertIsInstance(s[block], dict)

    def test_schema_name_matches_live(self) -> None:
        snapshots = _build_day(n_bars=2)
        self.assertEqual(snapshots[0]["schema_name"], "MarketSnapshot")


if __name__ == "__main__":
    unittest.main()
