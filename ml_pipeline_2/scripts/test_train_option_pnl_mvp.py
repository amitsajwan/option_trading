"""Unit tests for the trainer's single-position simulation.

The realistic per-recipe expectation depends on this honoring exit_bar_offset
correctly. Bugs here would silently inflate or deflate the "what should the
runtime produce" baseline, which is the primary metric we use to decide
deployment-readiness.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    HOLDOUT_END,
    load_params_override,
    simulate_single_position,
    split_temporal,
)


def _row(date, minute, pnl, hold):
    return {
        "trade_date": pd.Timestamp(date),
        "timestamp_minute": minute,
        "net_pnl_pct": pnl,
        "exit_bar_offset": hold,
    }


def test_single_position_fires_first_crossing_then_blocks():
    """Two prob-crossings within hold window: only first fires."""
    df = pd.DataFrame([
        _row("2024-08-01", 600, 0.05, 15),
        _row("2024-08-01", 605, 0.07, 15),  # blocked: 600+15=615 > 605
        _row("2024-08-01", 614, 0.03, 15),  # blocked
        _row("2024-08-01", 616, 0.10, 15),  # passes: 600+15=615 ≤ 616
    ])
    probs = np.array([0.60, 0.65, 0.70, 0.62])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == 0.05 + 0.10
    assert out["win_rate"] == 1.0


def test_single_position_resets_on_date_boundary():
    """Position state must reset at trade_date boundary (overnight gap)."""
    df = pd.DataFrame([
        _row("2024-08-01", 900, 0.04, 30),  # would block until 930
        _row("2024-08-02", 915, 0.06, 15),  # different day → fires
    ])
    probs = np.array([0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == 0.04 + 0.06


def test_single_position_skips_below_threshold():
    df = pd.DataFrame([
        _row("2024-08-01", 600, 0.05, 15),
        _row("2024-08-01", 700, 0.03, 15),
    ])
    probs = np.array([0.40, 0.60])  # first below thr, second above
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 1
    assert out["net_pnl_sum"] == 0.03


def test_single_position_uses_actual_hold_bars():
    """If exit_bar_offset is short (early stop hit), next fire allowed sooner."""
    df = pd.DataFrame([
        _row("2024-08-01", 600, -0.10, 3),   # stopped out at +3 bars
        _row("2024-08-01", 604, 0.05, 15),  # 600+3=603 ≤ 604 → fires
        _row("2024-08-01", 606, 0.07, 15),  # blocked (604+15=619 > 606)
    ])
    probs = np.array([0.60, 0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 2
    assert out["net_pnl_sum"] == -0.10 + 0.05


def test_single_position_empty_input():
    df = pd.DataFrame([_row("2024-08-01", 600, 0.05, 15)])
    out = simulate_single_position(df, np.array([0.30]), threshold=0.55)
    assert out["n_trades"] == 0
    assert out["net_pnl_sum"] == 0.0
    assert out["win_rate"] == 0.0


def test_single_position_correctly_orders_rows():
    """Even if input rows are scrambled, simulation must walk in time order."""
    df = pd.DataFrame([
        _row("2024-08-01", 700, 0.05, 15),
        _row("2024-08-01", 600, 0.07, 15),  # earlier — should fire first
        _row("2024-08-01", 800, 0.03, 15),
    ])
    probs = np.array([0.60, 0.65, 0.70])
    out = simulate_single_position(df, probs, threshold=0.55)
    # First fire at minute 600 (pnl 0.07, blocks to 615);
    # 700 ≥ 615 → fires (pnl 0.05, blocks to 715);
    # 800 ≥ 715 → fires (pnl 0.03)
    assert out["n_trades"] == 3
    assert out["net_pnl_sum"] == 0.07 + 0.05 + 0.03


def test_single_position_win_rate_calculation():
    df = pd.DataFrame([
        _row("2024-08-01", 600, +0.10, 15),
        _row("2024-08-02", 600, -0.05, 15),
        _row("2024-08-03", 600, +0.02, 15),
    ])
    probs = np.array([0.60, 0.60, 0.60])
    out = simulate_single_position(df, probs, threshold=0.55)
    assert out["n_trades"] == 3
    assert out["win_rate"] == pytest_approx(2 / 3)


# Tiny local approx helper to avoid importing pytest in this scope when run
# under bare unittest. Actual pytest is invoked via test command.
def pytest_approx(value, rel=1e-6):
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= max(abs(value), abs(other)) * rel
        def __repr__(self):
            return f"~={value}"
    return _Approx()


# -- load_params_override ------------------------------------------------------
# These tests gate the apples-to-apples HPO trial-18 comparison. If params
# loading silently picks wrong values, the trainer "realistic" verdict no
# longer matches the deployed bundle, and our deployment decision is wrong.

def test_params_override_flat_dict(tmp_path: Path):
    p = tmp_path / "params.json"
    payload = {"max_depth": 8, "learning_rate": 0.01, "n_estimators": 300}
    p.write_text(json.dumps(payload))
    assert load_params_override(p) == payload


def test_params_override_hpo_results_picks_first_trial(tmp_path: Path):
    """HPO writes trials best-first; loader must pull trials[0].params."""
    p = tmp_path / "hpo.json"
    hpo_blob = {
        "best_trial_id": 18,
        "trials": [
            {"trial_id": 18, "params": {"max_depth": 8, "learning_rate": 0.01,
                                         "subsample": 0.85}},
            {"trial_id": 4, "params": {"max_depth": 4, "learning_rate": 0.05}},
        ],
    }
    p.write_text(json.dumps(hpo_blob))
    out = load_params_override(p)
    assert out == {"max_depth": 8, "learning_rate": 0.01, "subsample": 0.85}


def test_params_override_hpo_empty_trials_raises(tmp_path: Path):
    p = tmp_path / "hpo.json"
    p.write_text(json.dumps({"trials": []}))
    with pytest.raises(ValueError, match="trials.*no 'params'"):
        load_params_override(p)


def test_params_override_hpo_trial_missing_params_raises(tmp_path: Path):
    p = tmp_path / "hpo.json"
    p.write_text(json.dumps({"trials": [{"trial_id": 1}]}))
    with pytest.raises(ValueError, match="trials.*no 'params'"):
        load_params_override(p)


def test_params_override_rejects_non_dict(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="expected JSON object"):
        load_params_override(p)


def test_split_temporal_respects_holdout_end_override():
    """When holdout_end is tightened (e.g. 2024-09-30), Oct rows must drop out
    of holdout. Apples-to-apples replay comparison depends on this."""
    df = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-04-30",  # last train day
            "2024-05-01",  # first valid day
            "2024-07-31",  # last valid day
            "2024-08-01",  # first holdout day
            "2024-09-30",  # last day we want (custom holdout_end)
            "2024-10-15",  # excluded by custom holdout_end
            "2024-10-31",  # default holdout_end
        ]),
    })
    # Default holdout_end keeps both October rows
    _, _, hold_default = split_temporal(df)
    assert len(hold_default) == 4
    # Tightened holdout_end drops October rows
    _, _, hold_tight = split_temporal(df, holdout_end=pd.Timestamp("2024-09-30"))
    assert len(hold_tight) == 2
    assert hold_tight["trade_date"].max() == pd.Timestamp("2024-09-30")


def test_params_override_returns_new_dict_not_alias(tmp_path: Path):
    """Caller mutating result must not propagate to underlying file's reload."""
    p = tmp_path / "params.json"
    p.write_text(json.dumps({"max_depth": 8}))
    out = load_params_override(p)
    out["max_depth"] = 99  # mutate
    assert load_params_override(p) == {"max_depth": 8}
