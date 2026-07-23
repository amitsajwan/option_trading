"""Tests for the close_spread retry-idempotency fix (found 2026-07-22 review).

Before the fix: if a short leg's buy-back failed AFTER an earlier short leg's
buy-back had already succeeded, close_spread() returned None (correct, retry
needed) but the NEXT attempt re-submitted the buy-back for the already-flat
leg too, opening a real unintended position every retry. Separately, a failed
hedge (long) leg sell was recorded as price 0.0 and the close was reported as
fully successful, dropping the spread from tracking with a real leg still open.
"""
from __future__ import annotations

import datetime

from strategy_app.seller.executor import FilledLeg, OpenSpread, SafeExecutor
from strategy_app.seller.gateway import Fill


def _condor(closed_legs=None) -> OpenSpread:
    legs = [
        FilledLeg(action="SELL", option_type="CE", strike=57300, qty=15, entry_price=80.0),
        FilledLeg(action="SELL", option_type="PE", strike=57100, qty=15, entry_price=75.0),
        FilledLeg(action="BUY", option_type="CE", strike=57500, qty=15, entry_price=20.0),
        FilledLeg(action="BUY", option_type="PE", strike=56900, qty=15, entry_price=18.0),
    ]
    return OpenSpread(
        spread_id="sp1", structure="iron_condor", expiry="2026-07-28", qty=15, legs=legs,
        entry_credit=round((80.0 + 75.0) - (20.0 + 18.0), 2), width=400,
        opened_at="2026-07-22T10:00:00+05:30", trade_date="2026-07-22",
        closed_legs=dict(closed_legs or {}),
    )


class _ScriptedGateway:
    """Replays a fixed sequence of Fill results per (action, option_type, strike);
    counts calls per leg so a test can assert a leg was never re-submitted."""

    def __init__(self, script: dict[tuple[str, str, int], list[Fill]]):
        self._script = {k: list(v) for k, v in script.items()}
        self.calls: dict[tuple[str, str, int], int] = {}

    def execute(self, action, option_type, strike, expiry, qty) -> Fill:
        key = (action, option_type, strike)
        self.calls[key] = self.calls.get(key, 0) + 1
        queue = self._script.get(key)
        if not queue:
            raise AssertionError(f"unexpected/unscripted call: {key} (already resolved leg re-submitted?)")
        return queue.pop(0)


def test_short_leg_failure_does_not_retry_already_closed_short():
    """CE short buy-back succeeds, PE short buy-back fails -> retry.
    On retry, CE must NOT be re-submitted (it's already flat)."""
    spread = _condor()
    gw = _ScriptedGateway({
        ("BUY", "CE", 57300): [Fill(True, 12.0)],
        ("BUY", "PE", 57100): [Fill(False, 0.0, error="RMS timeout"), Fill(True, 10.0)],
        ("SELL", "CE", 57500): [Fill(True, 5.0)],
        ("SELL", "PE", 56900): [Fill(True, 4.0)],
    })
    ex = SafeExecutor(gw, qty=15, width=400)
    expiry = datetime.date(2026, 7, 28)

    first = ex.close_spread(spread, expiry)
    assert first is None
    assert gw.calls[("BUY", "CE", 57300)] == 1
    assert gw.calls[("BUY", "PE", 57100)] == 1
    assert spread.closed_legs.get("CE57300") == 12.0
    assert "PE57100" not in spread.closed_legs

    second = ex.close_spread(spread, expiry)
    assert second is not None
    # CE short must NOT have been re-submitted on the retry.
    assert gw.calls[("BUY", "CE", 57300)] == 1
    assert gw.calls[("BUY", "PE", 57100)] == 2


def test_failed_hedge_leg_sell_retries_instead_of_silently_succeeding():
    """Both shorts close fine; the CE hedge sell fails. Must return None (retry),
    not a fabricated exit_value with the failed leg recorded as price 0."""
    spread = _condor()
    gw = _ScriptedGateway({
        ("BUY", "CE", 57300): [Fill(True, 12.0)],
        ("BUY", "PE", 57100): [Fill(True, 10.0)],
        ("SELL", "CE", 57500): [Fill(False, 0.0, error="RMS timeout")],
        ("SELL", "PE", 56900): [Fill(True, 4.0)],
    })
    ex = SafeExecutor(gw, qty=15, width=400)
    expiry = datetime.date(2026, 7, 28)

    result = ex.close_spread(spread, expiry)
    assert result is None
    # Both shorts (risk-reducing) succeeded and must be recorded so they're
    # never re-submitted, even though the overall close is still pending.
    assert spread.closed_legs.get("CE57300") == 12.0
    assert spread.closed_legs.get("PE57100") == 10.0
    assert "CE57500" not in spread.closed_legs


def test_close_resumes_cleanly_from_prior_partial_progress():
    """Simulates a restart: closed_legs already has the shorts recorded
    (as if persisted after a prior partial attempt). A fresh close_spread()
    call must only submit the two remaining hedge legs."""
    spread = _condor(closed_legs={"CE57300": 12.0, "PE57100": 10.0})
    gw = _ScriptedGateway({
        ("SELL", "CE", 57500): [Fill(True, 5.0)],
        ("SELL", "PE", 56900): [Fill(True, 4.0)],
    })
    ex = SafeExecutor(gw, qty=15, width=400)
    expiry = datetime.date(2026, 7, 28)

    result = ex.close_spread(spread, expiry)
    assert result is not None
    assert ("BUY", "CE", 57300) not in gw.calls
    assert ("BUY", "PE", 57100) not in gw.calls
    assert gw.calls[("SELL", "CE", 57500)] == 1
    assert gw.calls[("SELL", "PE", 56900)] == 1


def test_full_success_computes_correct_exit_value():
    spread = _condor()
    gw = _ScriptedGateway({
        ("BUY", "CE", 57300): [Fill(True, 12.0)],
        ("BUY", "PE", 57100): [Fill(True, 10.0)],
        ("SELL", "CE", 57500): [Fill(True, 5.0)],
        ("SELL", "PE", 56900): [Fill(True, 4.0)],
    })
    ex = SafeExecutor(gw, qty=15, width=400)
    expiry = datetime.date(2026, 7, 28)

    result = ex.close_spread(spread, expiry)
    # exit_value = (shorts bought back) - (longs sold) = (12+10) - (5+4) = 13
    assert result == 13.0
