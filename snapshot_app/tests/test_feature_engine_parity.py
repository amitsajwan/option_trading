"""
Parity harness — feature_engine vs the legacy live path (runtime_features).

This is the cutover decision artifact, frozen as a regression test. It proves
that build_features() reproduces the existing live feature computation EXACTLY
on the shared columns, and pins the handful of deliberate divergences (which
are pre-existing train/live skews that unifying onto feature_engine fixes).

If a future edit changes a MATCH column, this test fails loudly — that is the
guard against silently re-introducing skew.
"""

from __future__ import annotations

from datetime import timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from snapshot_app.core.runtime_features import _add_group_features
from snapshot_app.core.feature_engine import build_features

IST = timezone(timedelta(hours=5, minutes=30))

# legacy_name -> contract_name : these MUST be byte-identical between paths.
_MATCH_PAIRS = {
    "ret_1m": "ret_1m", "ret_3m": "ret_3m", "ret_5m": "ret_5m",
    "ema_9": "ema_9", "ema_21": "ema_21", "ema_50": "ema_50",
    "ema_9_slope": "ema_9_slope", "ema_50_slope": "ema_50_slope",
    "atr_14": "osc_atr_14", "atr_ratio": "osc_atr_ratio",
    "vwap_distance": "vwap_distance",
    "basis": "dist_basis", "basis_change_1m": "dist_basis_change_1m",
    "fut_rel_volume_20": "fut_flow_rel_volume_20",
    "fut_oi_rel_20": "fut_flow_oi_rel_20",
    "fut_oi_zscore_20": "fut_flow_oi_zscore_20",
    "fut_oi_change_1m": "fut_flow_oi_change_1m",
    "fut_oi_change_5m": "fut_flow_oi_change_5m",
    "atm_call_return_1m": "opt_flow_atm_call_return_1m",
    "atm_put_return_1m": "opt_flow_atm_put_return_1m",
    "atm_oi_change_1m": "opt_flow_atm_oi_change_1m",
    "atm_oi_ratio": "atm_oi_ratio", "near_atm_oi_ratio": "near_atm_oi_ratio",
    "ce_pe_oi_diff": "opt_flow_ce_pe_oi_diff",
    "ce_pe_volume_diff": "opt_flow_ce_pe_volume_diff",
    "options_volume_total": "opt_flow_options_volume_total",
    "options_rel_volume_20": "opt_flow_rel_volume_20",
    "pcr_change_5m": "pcr_change_5m", "pcr_change_15m": "pcr_change_15m",
    "opening_range_breakout_up": "ctx_opening_range_breakout_up",
    "opening_range_breakout_down": "ctx_opening_range_breakout_down",
    # formerly-divergent skews, now converged onto feature_engine:
    "ema_9_21_spread": "ema_9_21_spread",
    "rsi_14": "osc_rsi_14",
    "distance_from_day_high": "dist_from_day_high",
    "distance_from_day_low": "dist_from_day_low",
    "iv_skew": "iv_skew",
}

# Formerly-divergent columns — these WERE train/live skews; the convergence
# commit fixed runtime_features + RollingFeatureState to match feature_engine.
# They are now MATCH columns. This map documents what was fixed and asserts the
# fix is not silently reverted (test_former_skews_now_converge).
_FIXED_SKEWS = {
    "ema_9_21_spread": "ema_9_21_spread",          # raw -> (ema9-ema21)/close
    "rsi_14": "osc_rsi_14",                         # local def -> shared feature_engine._rsi
    "distance_from_day_high": "dist_from_day_high",  # (close-high)/high -> (high-close)/close
    "distance_from_day_low": "dist_from_day_low",    # (close-low)/low -> (close-low)/close
    "iv_skew": "iv_skew",                            # normalized+clipped -> raw ce_iv-pe_iv
}


def _panel(n: int = 120, seed: int = 11) -> pd.DataFrame:
    ts = pd.date_range("2024-01-03 09:15", periods=n, freq="1min", tz=IST)
    rng = np.random.default_rng(seed)
    close = 46000 + rng.standard_normal(n).cumsum() * 30

    def w(b, s=1.0):
        return b + rng.standard_normal(n).cumsum() * s

    return pd.DataFrame({
        "timestamp": ts, "trade_date": "2024-01-03",
        "fut_open": close - 3, "fut_high": close + 15, "fut_low": close - 15, "fut_close": close,
        "fut_volume": rng.integers(5e4, 2e5, n).astype(float),
        "fut_oi": (1.2e6 + rng.integers(-5e3, 5e3, n).cumsum()).astype(float),
        "spot_close": close - 5,
        "ce_oi_total": w(1e6, 500), "pe_oi_total": w(9e5, 500), "pcr_oi": rng.uniform(.8, 1.2, n),
        "ce_volume_total": rng.integers(1e5, 5e5, n).astype(float),
        "pe_volume_total": rng.integers(1e5, 5e5, n).astype(float),
        "opt_0_ce_close": w(200, 5), "opt_0_pe_close": w(190, 5),
        "opt_0_ce_oi": w(5e5, 200), "opt_0_pe_oi": w(5e5, 200),
        "opt_m1_ce_oi": w(4e5, 150), "opt_m1_pe_oi": w(4e5, 150),
        "opt_p1_ce_oi": w(4e5, 150), "opt_p1_pe_oi": w(4e5, 150),
        "opt_0_ce_iv": rng.uniform(.12, .18, n), "opt_0_pe_iv": rng.uniform(.13, .19, n),
        "atm_strike": np.full(n, 46000.0),
    })


def _run_both(panel: pd.DataFrame):
    old = _add_group_features(panel.copy()).reset_index(drop=True)
    new = build_features(
        panel.copy().set_index("timestamp"),
        layers=["0_normalise", "1_returns", "2_technicals", "2b_flow", "3_session"],
    ).reset_index(drop=True)
    return old, new


@pytest.mark.parametrize("legacy,contract", list(_MATCH_PAIRS.items()))
def test_parity_match_columns(legacy, contract):
    """Every shared column must be byte-identical between legacy and feature_engine."""
    old, new = _run_both(_panel())
    a = pd.to_numeric(old[legacy], errors="coerce")
    b = pd.to_numeric(new[contract], errors="coerce")
    both = a.notna() & b.notna()
    assert both.sum() > 0, f"{legacy}: no overlapping non-NaN values to compare"
    max_abs_diff = float((a[both] - b[both]).abs().max())
    assert max_abs_diff < 1e-6, (
        f"{legacy} -> {contract} diverged (max_abs_diff={max_abs_diff:.6g}); "
        f"feature_engine must reproduce the legacy live computation exactly. "
        f"If this change is intentional, move it to _EXPECTED_DIVERGENCES with a reason."
    )


@pytest.mark.parametrize("legacy,contract", list(_FIXED_SKEWS.items()))
def test_former_skews_now_converge(legacy, contract):
    """The 5 pre-existing train/live skews are now fixed: runtime_features +
    RollingFeatureState were converged onto feature_engine. Each must match
    EXACTLY. If one regresses, this fails — guarding the convergence."""
    old, new = _run_both(_panel())
    assert legacy in old.columns and contract in new.columns
    a = pd.to_numeric(old[legacy], errors="coerce")
    b = pd.to_numeric(new[contract], errors="coerce")
    both = a.notna() & b.notna()
    assert both.sum() > 0
    max_abs_diff = float((a[both] - b[both]).abs().max())
    assert max_abs_diff < 1e-9, (
        f"{legacy} -> {contract} diverged again (max_abs_diff={max_abs_diff:.3e}); "
        f"the skew-fix in runtime_features/RollingFeatureState was reverted."
    )
