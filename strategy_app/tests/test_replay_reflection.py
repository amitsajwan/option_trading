"""The sim replay surfaces the deterministic loss tag per trade (observability)."""

from __future__ import annotations

from strategy_app.sim.replay_engine import _closed_trade_reflection


def _cp(**kw):
    base = dict(pnl_pct=-0.10, mfe_pct=0.0, mae_pct=-0.20,
                target_pct=0.40, stop_loss_pct=0.20, exit_reason="stop_loss")
    base.update(kw)
    return base


def _entry(**kw):
    base = dict(direction="CE", prem_in=200.0, lots=1)
    base.update(kw)
    return base


def test_cost_miss_surfaced():
    # +0.5% gross flipped negative by costs (1-lot @200 prem)
    tag, needs = _closed_trade_reflection(_cp(pnl_pct=-0.008, mfe_pct=0.05), _entry(), exit_prem=199.0)
    assert tag == "cost_miss"
    assert needs is False


def test_exit_miss_surfaced():
    # reached 90% of the 0.40 target then closed red
    tag, _ = _closed_trade_reflection(_cp(pnl_pct=-0.05, mfe_pct=0.36), _entry(), exit_prem=150.0)
    assert tag == "exit_miss"


def test_direction_miss_surfaced():
    tag, _ = _closed_trade_reflection(_cp(pnl_pct=-0.20, mfe_pct=0.05), _entry(), exit_prem=160.0)
    assert tag == "direction_miss"


def test_ambiguous_needs_reasoning():
    # mid-MFE, sizeable loss, no captured verdicts -> noise + needs_reasoning
    tag, needs = _closed_trade_reflection(_cp(pnl_pct=-0.15, mfe_pct=0.18), _entry(), exit_prem=170.0)
    assert tag == "noise"
    assert needs is True


def test_robust_to_missing_fields():
    # degrades quietly, never raises
    assert _closed_trade_reflection({}, {}, exit_prem=0.0) == ("", False)
