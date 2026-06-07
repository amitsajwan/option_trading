"""Tests for the exit simulator (board B-4.1 — prove the giveback fix)."""
from __future__ import annotations

from strategy_app.position.exit_sim import ExitParams, simulate_exit

P = ExitParams()   # premium 180, delta 0.5, hard_stop 0.045, giveback 0.5, min_mfe 0.02


def test_no_path():
    assert simulate_exit("CE", [], P)["reason"] == "no_path"


def test_loser_is_cut_at_hard_stop():
    # CE but underlying drops hard on bar 0 -> hard stop caps the loss
    path = [(-5.0, -40.0, -35.0), (-35.0, -60.0, -55.0)]
    r = simulate_exit("CE", path, P)
    assert r["reason"] == "hard_stop" and r["exit_pct"] == -P.hard_stop_pct
    # holding to time-stop would be much worse
    ts = simulate_exit("CE", path, P, time_stop_only=True)
    assert ts["exit_pct"] < r["exit_pct"]


def test_spike_then_revert_giveback_beats_timestop():
    # CE: bar0 spikes up intrabar (+60 high) then closes flat; bar1 fades to 0
    path = [(60.0, 0.0, 4.0), (5.0, -10.0, 0.0)]
    gb = simulate_exit("CE", path, P)
    ts = simulate_exit("CE", path, P, time_stop_only=True)
    assert gb["reason"] == "mfe_giveback"
    assert gb["exit_pct"] > ts["exit_pct"]      # trailing captured the spike; holding gave it back


def test_sustained_winner_positive_both_ways():
    # CE: steady climb, no big giveback
    path = [(20.0 * (k + 1) + 5, 20.0 * k, 20.0 * (k + 1)) for k in range(10)]
    gb = simulate_exit("CE", path, P)
    ts = simulate_exit("CE", path, P, time_stop_only=True)
    assert gb["exit_pct"] > 0 and ts["exit_pct"] > 0


def test_pe_side_mirrors():
    # PE wins when underlying falls: a sustained down move is favourable
    path = [(-20.0 * k, -20.0 * (k + 1) - 5, -20.0 * (k + 1)) for k in range(10)]
    r = simulate_exit("PE", path, P, time_stop_only=True)
    assert r["exit_pct"] > 0      # PE profited from the decline
    # the same down path is a loser for CE -> hard stop
    assert simulate_exit("CE", path, P)["reason"] == "hard_stop"
