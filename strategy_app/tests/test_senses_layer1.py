"""Unit tests for the Layer-1 senses + the shared context builder (board B-1.1..B-1.4)."""
from __future__ import annotations

from strategy_app.senses import SenseVerdict
from strategy_app.senses.context import WARMUP, build_contexts
from strategy_app.senses.cost_ev import CostEvSense
from strategy_app.senses.destination import DestinationSense
from strategy_app.senses.direction import DirectionSense, PlaceholderDirection, UNKNOWN
from strategy_app.senses.flow import FlowSense
from strategy_app.senses.move import MoveSense
from strategy_app.senses.regime import RegimeSense
from strategy_app.senses.risk import RiskSense


def _loaded_ctx(**over):
    """A context that triggers loaded = compression AND oi_build."""
    ctx = {
        "close": 54000.0, "atr_build": 10.0, "atr_base": 25.0,   # ratio 0.4 -> compressed
        "last_bar_return": 3.0, "option_volume": 1200.0, "vol_build_avg": 1000.0,
        "oi_now": 101000.0, "oi_15ago": 100000.0,                 # +1% > OI_BUILD
        "expected_move_pt": 117.0,
        "max_pain": 54600.0, "ce_oi_top_strike": 55200.0, "pe_oi_top_strike": 53000.0,
        "prior_day_high": 55000.0, "prior_day_low": 53000.0,
        "opening_range_high": 54400.0, "opening_range_low": 53600.0,
    }
    ctx.update(over)
    return ctx


# ---- Move ----

def test_move_loaded_pair_fires():
    v = MoveSense().evaluate(_loaded_ctx())
    assert v.verdict in ("loaded", "released")
    assert v.evidence["compression"] and v.evidence["oi_build"]
    assert v.evidence["prob_100"] == 0.49 and v.evidence["expected_move_pt"] == 117.0


def test_move_quiet_without_oi_build():
    v = MoveSense().evaluate(_loaded_ctx(oi_now=100000.0, oi_15ago=100000.0))
    assert v.verdict == "quiet"
    assert v.evidence["expected_move_pt"] == 93.0   # base calibration


def test_move_abstains_without_baseline():
    assert MoveSense().evaluate({"atr_base": 0.0}).is_abstain


def test_move_released_needs_velocity_or_volume():
    # big return -> velocity True -> released
    v = MoveSense().evaluate(_loaded_ctx(last_bar_return=100.0, atr_build=10.0))
    assert v.evidence["velocity"] and v.evidence["released"] and v.verdict == "released"


# ---- Destination ----

def test_destination_room_when_walls_far():
    v = DestinationSense().evaluate(_loaded_ctx())
    assert v.verdict == "room"
    assert v.evidence["space_to_move_ratio"] >= 1.0


def test_destination_no_room_when_wall_close():
    # resistance just 50pt away, expected move 117 -> no room
    v = DestinationSense().evaluate(_loaded_ctx(
        max_pain=54050.0, ce_oi_top_strike=54050.0, opening_range_high=54050.0, prior_day_high=54050.0))
    assert v.verdict == "no_room"
    assert v.evidence["space_to_move_ratio"] < 1.0


def test_destination_uses_weekly_level_as_wall():
    # §12.1 — a nearby weekly high becomes the nearest resistance and shrinks the room.
    # No prior-day / ORB walls above; week_high 54050 is the only wall above close 54000.
    ctx = {"close": 54000.0, "expected_move_pt": 117.0,
           "prior_day_low": 53000.0, "week_high": 54050.0, "week_low": 52000.0}
    v = DestinationSense().evaluate(ctx)
    assert v.evidence["nearest_resistance"] == 54050.0   # the weekly high
    assert v.verdict == "no_room"                         # 50pt room < 117pt move


def test_destination_abstains_without_levels():
    v = DestinationSense().evaluate({"close": 54000.0, "expected_move_pt": 117.0})
    assert v.is_abstain


# ---- Regime ----

def test_regime_states_across_ratio():
    R = RegimeSense()
    assert R.evaluate({"atr_build": 1.0, "atr_base": 10.0}).verdict == "dead"        # 0.1
    assert R.evaluate({"atr_build": 5.0, "atr_base": 10.0}).verdict == "compressed"  # 0.5
    assert R.evaluate({"atr_build": 10.0, "atr_base": 10.0}).verdict == "alive"      # 1.0
    assert R.evaluate({"atr_build": 20.0, "atr_base": 10.0}).verdict == "expanding"  # 2.0
    assert R.evaluate({"atr_build": 40.0, "atr_base": 10.0}).verdict == "chaotic"    # 4.0


# ---- Cost/EV ----

def test_cost_ev_breakdown_and_sign():
    v = CostEvSense().evaluate({"expected_move_pt": 117.0})
    assert v.evidence["gross_if_right_pct"] > 0
    assert v.evidence["gross_if_wrong_pct"] < 0
    assert 0.005 < v.evidence["cost_pct"] < 0.03      # ~0.9-1.3% round trip on default premium/lot
    assert v.evidence["calibration"].startswith("empirical-anchor")
    # the exit-giveback asymmetry: wrong-side loss must exceed the right-side gain
    assert abs(v.evidence["gross_if_wrong_pct"]) > v.evidence["gross_if_right_pct"]
    assert v.evidence["asymmetry"] > 1.0


def test_cost_ev_phase4_capture_lever_lowers_asymmetry():
    # modelling better exits (mfe_capture>1) raises right-side and shrinks the asymmetry
    base = CostEvSense().evaluate({"expected_move_pt": 117.0})
    better = CostEvSense(mfe_capture=2.0).evaluate({"expected_move_pt": 117.0})
    assert better.evidence["gross_if_right_pct"] > base.evidence["gross_if_right_pct"]
    assert better.evidence["asymmetry"] < base.evidence["asymmetry"]


def test_cost_ev_abstains_without_move():
    assert CostEvSense().evaluate({}).is_abstain


# ---- Risk ----

def test_risk_blocks_on_drawdown_and_position():
    assert RiskSense().evaluate({"daily_dd": -0.07}).verdict == "blocked"
    assert RiskSense().evaluate({"consec_losses": 3}).verdict == "blocked"
    assert RiskSense().evaluate({"in_position": True}).verdict == "blocked"
    assert RiskSense().evaluate({"daily_dd": -0.01, "consec_losses": 1}).verdict == "ok"


# ---- Flow ----

def test_flow_abstains_without_depth_and_classifies_with_it():
    assert FlowSense().evaluate({}).is_abstain
    assert FlowSense().evaluate({"net_ofi": 0.4}).verdict == "bull"
    assert FlowSense().evaluate({"net_ofi": -0.4}).verdict == "bear"
    assert FlowSense().evaluate({"net_ofi": 0.05}).verdict == "neutral"


# ---- Direction ----

def test_direction_abstains_without_vwap():
    v = DirectionSense().evaluate({})
    assert v.verdict == UNKNOWN and v.confidence == 0.0


def test_direction_vwap_plus_momentum_agree_gives_side():
    # price above vwap + 5m momentum up -> CE, higher confidence
    v = DirectionSense().evaluate({"close": 54050.0, "vwap": 54000.0, "fut_return_5m": 0.001})
    assert v.verdict == "CE" and v.confidence == 0.60 and "momentum_5m" in v.evidence["basis"]
    # below vwap + momentum down -> PE
    v2 = DirectionSense().evaluate({"close": 53950.0, "vwap": 54000.0, "fut_return_5m": -0.001})
    assert v2.verdict == "PE"


def test_direction_abstains_when_vwap_and_momentum_disagree():
    # above vwap but momentum down -> conflict -> UNKNOWN (D5)
    v = DirectionSense().evaluate({"close": 54050.0, "vwap": 54000.0, "fut_return_5m": -0.001})
    assert v.verdict == UNKNOWN and "disagree" in v.evidence["reason"]


def test_direction_vwap_only_when_momentum_flat():
    v = DirectionSense().evaluate({"close": 54050.0, "vwap": 54000.0, "fut_return_5m": 0.0})
    assert v.verdict == "CE" and v.confidence == 0.55 and v.evidence["basis"] == ["vwap"]


def test_placeholder_direction_supplies_side():
    v = PlaceholderDirection("PE").evaluate({})
    assert v.verdict == "PE" and v.confidence == 1.0


# ---- context builder ----

def test_build_contexts_windowing_and_warmup():
    bars = [{"c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "ovol": 100.0, "ooi": 1000.0 + i}
            for i in range(WARMUP + 15)]
    ctxs = build_contexts({"d": bars}, horizon=10)
    assert ctxs and all(c.index >= WARMUP for c in ctxs)
    c0 = ctxs[0]
    assert c0.atr_base > 0 and c0.atr_build > 0
    assert c0.future_move_pt is not None and c0.future_signed_move_pt is not None


def test_build_contexts_weekly_levels_flow_through():
    # §12.1 offline symmetry — week_high/low from levels reach the context mapping
    bars = [{"c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "ovol": 100.0, "ooi": 1000.0 + i}
            for i in range(WARMUP + 15)]
    levels = {"d": {"prior_day_high": 200.0, "prior_day_low": 50.0,
                    "week_high": 250.0, "week_low": 40.0}}
    m = build_contexts({"d": bars}, horizon=10, levels=levels)[0].as_mapping()
    assert m["week_high"] == 250.0 and m["week_low"] == 40.0


def test_build_contexts_detects_prior_day_sweep():
    # §12.2 offline symmetry — a bar that pierces PDH intrabar but closes back below sweeps up.
    # PDH=150; the warmup bars stay well below it, then bar at WARMUP pierces & rejects.
    bars = [{"c": 100.0, "h": 101.0, "l": 99.0, "ovol": 100.0, "ooi": 1000.0 + i}
            for i in range(WARMUP + 12)]
    swept_i = WARMUP
    bars[swept_i] = {"c": 149.0, "h": 152.0, "l": 99.0, "ovol": 100.0, "ooi": 1000.0 + swept_i}
    levels = {"d": {"prior_day_high": 150.0, "prior_day_low": 50.0}}
    ctxs = build_contexts({"d": bars}, horizon=10, levels=levels)
    swept = next(c for c in ctxs if c.index == swept_i)
    assert swept.struct_swept is True and swept.struct_sweep_direction == "up"
    assert swept.struct_fakeout is True       # routed to the trap verdict
