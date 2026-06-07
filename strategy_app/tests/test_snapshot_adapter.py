"""Tests for the live snapshot -> sense-context adapter (the context bridge)."""
from __future__ import annotations

from strategy_app.brain.decision_brain import DecisionBrain
from strategy_app.brain.sense_runner import run_senses
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.senses.cost_ev import CostEvSense
from strategy_app.senses.direction import PlaceholderDirection
from strategy_app.senses.snapshot_adapter import snapshot_to_sense_context


def _payload(*, vol_ratio=0.5, oi_change=5000.0, ret_1m=0.0008, realized_vol=0.0006,
             vol_volume_ratio=2.0, close=54000.0):
    return {
        "futures_bar": {"fut_close": close, "fut_high": close + 20, "fut_low": close - 20},
        "futures_derived": {
            "fut_return_1m": ret_1m, "realized_vol_30m": realized_vol,
            "vol_ratio": vol_ratio, "fut_volume_ratio": vol_volume_ratio,
            "fut_oi_change_30m": oi_change, "vwap": close - 10,
        },
        "opening_range": {"orh": 54400.0, "orl": 53600.0},
        "chain_aggregates": {"max_pain": 54600, "ce_oi_top_strike": 55200, "pe_oi_top_strike": 53000,
                             "total_ce_oi": 1_000_000, "total_pe_oi": 1_100_000},
        "atm_options": {"atm_ce_close": 190.0, "atm_pe_close": 185.0},
        "session_context": {"date": "2026-06-05"},
    }


def test_adapter_maps_core_fields():
    snap = SnapshotAccessor(_payload())
    ctx = snapshot_to_sense_context(snap)
    assert ctx["compression_ratio"] == 0.5          # from vol_ratio
    assert ctx["oi_change"] == 5000.0               # from fut_oi_change_30m
    # last bar return converted pct -> points
    assert abs(ctx["last_bar_return"] - 0.0008 * 54000.0) < 1e-6
    assert ctx["max_pain"] == 54600 and ctx["opening_range_high"] == 54400.0
    assert 180.0 <= ctx["atm_premium"] <= 195.0      # ATM premium (CE/PE blend)


def test_adapter_velocity_and_volume_flags():
    # |ret_1m|=0.0008 > 1.5*realized_vol(0.0006)=0.0009? no -> velocity False
    ctx = snapshot_to_sense_context(SnapshotAccessor(_payload(ret_1m=0.0008, realized_vol=0.0006)))
    assert ctx["velocity_flag"] is False
    ctx2 = snapshot_to_sense_context(SnapshotAccessor(_payload(ret_1m=0.003, realized_vol=0.0006)))
    assert ctx2["velocity_flag"] is True            # 0.003 > 0.0009
    assert ctx["volume_flag"] is True               # fut_volume_ratio 2.0 > 1.8


def test_adapter_feeds_loaded_move_through_run_senses():
    # compression (vol_ratio 0.5 < 0.70) AND oi_build (oi_change > 0) -> loaded
    snap = SnapshotAccessor(_payload(vol_ratio=0.5, oi_change=8000.0))
    ctx = snapshot_to_sense_context(snap)
    verdicts = run_senses(ctx, direction_sense=PlaceholderDirection("CE"))
    assert verdicts["move"].verdict in ("loaded", "released")
    assert verdicts["move"].evidence["compression"] and verdicts["move"].evidence["oi_build"]
    assert verdicts["regime"].verdict in ("compressed", "dead")   # low vol_ratio
    assert not verdicts["cost_ev"].is_abstain                     # expected_move flowed through


def test_adapter_quiet_when_no_compression():
    snap = SnapshotAccessor(_payload(vol_ratio=1.2, oi_change=8000.0))   # vol above baseline
    verdicts = run_senses(snapshot_to_sense_context(snap))
    assert verdicts["move"].verdict == "quiet"


def test_adapter_maps_weekly_levels():
    # §12.1 — week_high/low flow from session_levels into the sense context
    payload = _payload()
    payload["session_levels"] = {"prev_day_high": 55000.0, "prev_day_low": 53000.0,
                                 "week_high": 55400.0, "week_low": 52600.0}
    ctx = snapshot_to_sense_context(SnapshotAccessor(payload))
    assert ctx["week_high"] == 55400.0 and ctx["week_low"] == 52600.0
    assert ctx["prior_day_high"] == 55000.0


def test_adapter_detects_prior_day_sweep_up():
    # §12.2 — bar pierces PDH intrabar (fut_high 54020 > PDH 54010) but closes back below it.
    payload = _payload(close=54000.0)   # fut_high = close+20 = 54020, fut_low = close-20 = 53980
    payload["session_levels"] = {"prev_day_high": 54010.0, "prev_day_low": 53000.0}
    ctx = snapshot_to_sense_context(SnapshotAccessor(payload))
    assert ctx["struct_swept"] is True
    assert ctx["struct_sweep_direction"] == "up"
    assert ctx["struct_fakeout"] is True          # a sweep IS a trap -> routes to fakeout
    # and that fakeout reaches the brain's conflict layer as loaded_into_fakeout
    verdicts = run_senses(ctx, direction_sense=PlaceholderDirection("CE"))
    assert verdicts["structure"].verdict == "fakeout"
    assert verdicts["structure"].evidence["sweep_direction"] == "up"


def test_adapter_no_sweep_when_close_holds_breakout():
    # close ABOVE PDH (a genuine breakout, not a swept-and-rejected trap) -> not a sweep
    payload = _payload(close=54000.0)
    payload["session_levels"] = {"prev_day_high": 53990.0, "prev_day_low": 53000.0}
    ctx = snapshot_to_sense_context(SnapshotAccessor(payload))
    assert ctx["struct_swept"] is False
    assert ctx["struct_sweep_direction"] == "none"


def test_brain_decides_end_to_end_on_live_snapshot():
    # full path: live snapshot -> adapter -> senses -> brain (defer_direction shadow mode)
    snap = SnapshotAccessor(_payload(vol_ratio=0.5, oi_change=8000.0))
    ctx = snapshot_to_sense_context(snap, risk_ctx={"daily_dd": -0.01, "consec_losses": 0, "in_position": False})
    # expected_move needed by destination -> run via run_senses
    verdicts = run_senses(ctx, direction_sense=PlaceholderDirection("CE"))
    decision = DecisionBrain(defer_direction=True).decide(verdicts)
    assert decision.action in ("TRADE", "WAIT", "SKIP", "NO_TRADE")
    assert decision.size == (1 if decision.action == "TRADE" else 0)
    assert "move" in decision.to_trace()["verdicts"]
