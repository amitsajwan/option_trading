"""Tests for option_pnl_predictor — covers bundle loading + decision building.

The dangerous failure modes we explicitly pin:
  - Bundle metadata version mismatch -> reject (don't run a broken bundle)
  - Bundle missing required files -> reject
  - Missing ATM strike at predict time -> still emits a clean decision (downstream blocks)
  - Predict probability below threshold -> action="HOLD" with reason encoding prob
  - Predict probability above threshold for PE bundle -> action="BUY_PE", risk_basis="option_premium"
  - Predict probability above threshold for CE bundle -> action="BUY_CE"
  - Strike offset for OTM/ITM recipes uses snap.strike_step
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import joblib
import numpy as np
import pytest

from strategy_app.ml.option_pnl_predictor import (
    OptionPnlBundle,
    build_decision_from_bundle,
    load_option_pnl_bundle,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor


# ── Helpers ────────────────────────────────────────────────────────────


def _write_minimal_bundle(
    tmp_path: Path,
    *,
    option_type: str = "PE",
    strike_offset_steps: int = 0,
    threshold: float = 0.55,
    feature_columns: Optional[list[str]] = None,
) -> Path:
    """Create a real on-disk bundle that load_option_pnl_bundle will accept."""
    bundle = tmp_path / "test_bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    feature_columns = feature_columns or ["f1", "f2", "f3"]
    # Small sklearn classifier so tests don't depend on xgboost being installed
    # locally. In production the bundle holds an XGBClassifier; both implement
    # the predict_proba interface the predictor relies on.
    from sklearn.linear_model import LogisticRegression
    model = LogisticRegression(max_iter=200)
    n = len(feature_columns)
    X = np.array(
        [
            np.linspace(0.1, 0.3, n),
            np.linspace(0.4, 0.6, n),
            np.zeros(n),
            np.ones(n),
        ] * 5,
        dtype=np.float32,
    )
    y = np.array([0, 1, 0, 1] * 5)
    model.fit(X, y)
    joblib.dump(model, bundle / "model.joblib")

    (bundle / "feature_columns.json").write_text(json.dumps({
        "feature_columns": feature_columns,
        "n_features": len(feature_columns),
    }))
    (bundle / "metadata.json").write_text(json.dumps({
        "bundle_version": "option_pnl_v1",
        "run_id": "test_run",
        "recipe_id": f"ATM_{option_type}_15",
        "decision_threshold": threshold,
        "recipe_params": {
            "option_type": option_type,
            "strike_offset_steps": strike_offset_steps,
            "max_hold_bars": 15,
            "stop_pct_of_premium": 0.25,
            "target_pct_of_premium": 0.40,
        },
    }))
    return bundle


def _fake_snap(*, atm_strike: Optional[int] = 50000, features: Optional[dict] = None):
    if features is not None:
        return SnapshotAccessor(features)
    return SnapshotAccessor(
        {
            "snapshot_id": "snap-1",
            "instrument": "BANKNIFTY-I",
            "session_context": {
                "snapshot_id": "snap-1",
                "timestamp": "2024-09-25T10:30:00+05:30",
                "date": "2024-09-25",
                "session_phase": "ACTIVE",
                "minutes_since_open": 75,
                "day_of_week": 2,
                "days_to_expiry": 1,
                "is_expiry_day": False,
            },
            "futures_bar": {
                "fut_open": 50000.0,
                "fut_high": 50100.0,
                "fut_low": 49900.0,
                "fut_close": 50050.0,
                "fut_volume": 120000.0,
                "fut_oi": 900000.0,
            },
            "chain_aggregates": {
                "atm_strike": atm_strike,
                "pcr": 1.08,
                "total_ce_oi": 1500000.0,
                "total_pe_oi": 1620000.0,
            },
            "atm_options": {
                "atm_ce_close": 210.0,
                "atm_pe_close": 195.0,
                "atm_ce_oi": 300000.0,
                "atm_pe_oi": 320000.0,
                "atm_ce_volume": 18000.0,
                "atm_pe_volume": 21000.0,
            },
            "velocity_enrichment": {
                "vel_ce_oi_delta_open": 1000.0,
                "ctx_gap_down": 0.0,
                "adx_14": 22.0,
                "vol_spike_ratio": 1.2,
            },
            "strikes": [
                {"strike": 49900, "ce_ltp": 260.0, "pe_ltp": 155.0},
                {"strike": 50000, "ce_ltp": 210.0, "pe_ltp": 195.0},
                {"strike": 50100, "ce_ltp": 170.0, "pe_ltp": 245.0},
            ],
        }
    )


def _rolling_features(**overrides):
    base = {
        "ret_1m": 0.001,
        "ret_3m": 0.002,
        "ret_5m": 0.003,
        "ema_9_21_spread": 12.0,
        "osc_rsi_14": 58.0,
        "osc_atr_ratio": 0.004,
        "osc_atr_daily_percentile": 0.65,
        "vwap_distance": 0.0015,
        "fut_rel_volume_20": 1.1,
        "fut_oi_change_1m": 100.0,
        "opt_flow_pcr_oi": 1.08,
        "pcr_change_5m": 0.03,
        "atm_call_return_1m": 0.02,
        "atm_put_return_1m": -0.01,
        "atm_oi_change_1m": 500.0,
        "atm_oi_ratio": 0.48,
        "near_atm_oi_ratio": 0.51,
        "ce_pe_oi_diff": -120000.0,
        "ce_pe_volume_diff": -3000.0,
        "options_volume_total": 39000.0,
        "time_minute_of_day": 630,
        "time_day_of_week": 2,
        "opening_range_ready": 1.0,
        "opening_range_breakout_up": 0.0,
        "opening_range_breakout_down": 0.0,
        "ctx_dte_days": 1.0,
        "ctx_is_expiry_day": 0.0,
        "ctx_is_near_expiry": 1.0,
        "ctx_is_high_vix_day": 0.0,
        "ctx_regime_atr_high": 0.0,
        "ctx_regime_atr_low": 0.0,
        "ctx_regime_trend_up": 1.0,
        "ctx_regime_trend_down": 0.0,
        "ctx_regime_expiry_near": 1.0,
    }
    base.update(overrides)
    return base


# ── Bundle loading ────────────────────────────────────────────────────


def test_load_bundle_happy_path(tmp_path: Path):
    bundle_dir = _write_minimal_bundle(tmp_path)
    bundle = load_option_pnl_bundle(bundle_dir)
    assert bundle.recipe_id == "ATM_PE_15"
    assert bundle.option_type == "PE"
    assert bundle.strike_offset_steps == 0
    assert bundle.max_hold_bars == 15
    assert bundle.decision_threshold == pytest.approx(0.55)
    assert len(bundle.feature_columns) == 3


def test_load_bundle_rejects_missing_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_option_pnl_bundle(tmp_path / "does_not_exist")


def test_load_bundle_rejects_missing_files(tmp_path: Path):
    bundle = tmp_path / "broken"
    bundle.mkdir()
    # missing model.joblib + others
    with pytest.raises(FileNotFoundError):
        load_option_pnl_bundle(bundle)


def test_load_bundle_rejects_wrong_version(tmp_path: Path):
    bundle_dir = _write_minimal_bundle(tmp_path)
    md = json.loads((bundle_dir / "metadata.json").read_text())
    md["bundle_version"] = "v2_future"
    (bundle_dir / "metadata.json").write_text(json.dumps(md))
    with pytest.raises(ValueError, match="unsupported bundle_version"):
        load_option_pnl_bundle(bundle_dir)


def test_load_bundle_rejects_bad_option_type(tmp_path: Path):
    bundle_dir = _write_minimal_bundle(tmp_path)
    md = json.loads((bundle_dir / "metadata.json").read_text())
    md["recipe_params"]["option_type"] = "XX"
    (bundle_dir / "metadata.json").write_text(json.dumps(md))
    with pytest.raises(ValueError, match="option_type must be CE or PE"):
        load_option_pnl_bundle(bundle_dir)


def test_load_bundle_rejects_missing_recipe_param(tmp_path: Path):
    bundle_dir = _write_minimal_bundle(tmp_path)
    md = json.loads((bundle_dir / "metadata.json").read_text())
    del md["recipe_params"]["stop_pct_of_premium"]
    (bundle_dir / "metadata.json").write_text(json.dumps(md))
    with pytest.raises(ValueError, match="stop_pct_of_premium"):
        load_option_pnl_bundle(bundle_dir)


# ── build_decision_from_bundle ────────────────────────────────────────


def test_decision_for_pe_bundle_action_and_risk_basis(tmp_path: Path):
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="PE"))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    # action is HOLD or BUY_PE — not BUY_CE (regression guard)
    assert decision.action in ("HOLD", "BUY_PE")
    assert decision.risk_basis == "option_premium"
    assert decision.recipe_id == "ATM_PE_15"


def test_decision_for_ce_bundle_emits_buy_ce(tmp_path: Path):
    """CE bundle must emit BUY_CE never BUY_PE — side mixing is a label bug."""
    # Force the model to predict high prob by setting threshold very low
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="CE", threshold=0.0))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "BUY_CE"
    assert decision.ce_prob == 1.0
    assert decision.pe_prob == 0.0


def test_decision_hold_when_below_threshold(tmp_path: Path):
    """Force threshold to 1.0 — model can never beat it, must return HOLD."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=1.0))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "HOLD"
    assert "prob_below_threshold" in decision.reason
    # Diagnostics still populated even on hold
    assert "option_pnl" in decision.model_diagnostics
    assert "predicted_prob" in decision.model_diagnostics["option_pnl"]


def test_decision_passes_recipe_params_through(tmp_path: Path):
    """Stop/target/horizon come from bundle, not engine defaults."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=0.0))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "BUY_PE"
    assert decision.stop_loss_pct == pytest.approx(0.25)
    assert decision.target_pct == pytest.approx(0.40)
    assert decision.horizon_minutes == 15


def test_decision_diagnostics_include_threshold_and_prob(tmp_path: Path):
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=0.5))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    d = decision.model_diagnostics["option_pnl"]
    assert d["threshold"] == pytest.approx(0.5)
    assert d["recipe_id"] == "ATM_PE_15"
    assert 0.0 <= d["predicted_prob"] <= 1.0
    assert d["feature_count"] == 3
    assert "stage1" in decision.model_diagnostics
    assert decision.model_diagnostics["stage1"]["input_hash"] == d["input_hash"]


def test_decision_handles_predict_exception_gracefully(tmp_path: Path):
    """If model.predict_proba raises, return HOLD instead of crashing the engine."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path))
    # Swap in a broken model
    broken_model = MagicMock()
    broken_model.predict_proba.side_effect = RuntimeError("simulated model crash")
    bundle = OptionPnlBundle(
        run_id=bundle.run_id, recipe_id=bundle.recipe_id,
        option_type=bundle.option_type, strike_offset_steps=bundle.strike_offset_steps,
        max_hold_bars=bundle.max_hold_bars,
        stop_pct_of_premium=bundle.stop_pct_of_premium,
        target_pct_of_premium=bundle.target_pct_of_premium,
        decision_threshold=bundle.decision_threshold,
        feature_columns=bundle.feature_columns,
        model=broken_model,
        metadata=bundle.metadata,
    )
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "HOLD"
    assert "option_pnl_predict_error" in decision.reason
    assert "simulated model crash" in decision.reason


def test_safe_float_handles_nan_and_none():
    """Feature extraction must fill nan/None with 0.0 — matches labeler training."""
    from strategy_app.ml.option_pnl_predictor import _safe_float
    assert _safe_float(None) == 0.0
    assert _safe_float(float("nan")) == 0.0
    assert _safe_float(float("inf")) == 0.0
    assert _safe_float(1.5) == 1.5
    assert _safe_float("not a number") == 0.0


def test_decision_with_missing_features_does_not_crash(tmp_path: Path):
    """If snap has only some of the required features, missing ones default to 0."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=0.0))
    snap = _fake_snap(features={"f1": 0.5})  # f2 and f3 missing
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    # No crash; either HOLD or BUY_PE based on what model predicts on [0.5, 0, 0]
    assert decision.action in ("HOLD", "BUY_PE")


def test_fire_decision_pre_selects_strike_atm(tmp_path: Path):
    """When the bundle decides to fire, decision.selected_strike must be set
    to the labeler's ATM rule — engine then bypasses smart-strike entirely.
    This is the fix for the 2024-08/09 holdout per-trade edge gap."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="PE",
                                                           strike_offset_steps=0,
                                                           threshold=0.0))
    snap = _fake_snap(atm_strike=50000)  # _fake_snap strikes 49900/50000/50100 → step=100
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "BUY_PE"
    assert decision.selected_strike == 50000  # exact ATM
    assert decision.selected_strike_reason == "bundle_atm"


def test_fire_decision_pre_selects_strike_otm_offset(tmp_path: Path):
    """OTM_1 PE: ATM - 1 * strike_step (per labeler convention)."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="PE",
                                                           strike_offset_steps=1,
                                                           threshold=0.0))
    snap = _fake_snap(atm_strike=50000)  # _fake_snap strikes 49900/50000/50100 → step=100
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "BUY_PE"
    # PE OTM goes DOWN from ATM
    assert decision.selected_strike == 49900
    assert decision.selected_strike_reason == "bundle_atm_offset_+1"


def test_fire_decision_otm_ce_goes_up(tmp_path: Path):
    """OTM_1 CE: ATM + 1 * strike_step. Direction sign matters."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="CE",
                                                           strike_offset_steps=1,
                                                           threshold=0.0))
    snap = _fake_snap(atm_strike=50000)  # _fake_snap strikes 49900/50000/50100 → step=100
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "BUY_CE"
    assert decision.selected_strike == 50100


def test_fire_decision_holds_when_atm_missing(tmp_path: Path):
    """If we can't pick the bundle's strike (no ATM), we MUST hold rather
    than fall back to smart-strike — that would violate labeler equivalence."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, option_type="PE",
                                                           threshold=0.0))
    snap = _fake_snap(atm_strike=None)
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "HOLD"
    assert decision.reason == "missing_atm_or_strike_step_for_bundle"
    assert decision.selected_strike is None


def test_hold_decision_does_not_set_strike(tmp_path: Path):
    """When prob < threshold, no strike is selected (no trade fires)."""
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=1.0))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap)
    assert decision.action == "HOLD"
    assert decision.selected_strike is None
    assert decision.selected_strike_reason is None


def test_flat_v2_features_are_extracted_from_nested_snapshot(tmp_path: Path):
    feature_columns = [
        "px_fut_close",
        "ret_1m",
        "opt_flow_pcr_oi",
        "ctx_dte_days",
        "vel_ce_oi_delta_open",
    ]
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=0.0, feature_columns=feature_columns))
    snap = _fake_snap()
    decision = build_decision_from_bundle(bundle=bundle, snap=snap, rolling_features=_rolling_features())
    d = decision.model_diagnostics["option_pnl"]
    assert d["feature_count"] == 5
    assert d["non_null_count"] == 5
    assert d["missing_count"] == 0
    assert d["input_hash"]
    assert decision.model_diagnostics["stage1"]["non_null_count"] == 5


def test_different_snapshots_produce_different_input_hashes(tmp_path: Path):
    feature_columns = ["px_fut_close", "ret_1m", "opt_flow_pcr_oi", "ctx_dte_days"]
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=0.0, feature_columns=feature_columns))
    d1 = build_decision_from_bundle(
        bundle=bundle,
        snap=_fake_snap(),
        rolling_features=_rolling_features(ret_1m=0.001),
    ).model_diagnostics["option_pnl"]
    snap2 = _fake_snap(features={
        **_fake_snap().raw_payload,
        "futures_bar": {
            **_fake_snap().raw_payload["futures_bar"],
            "fut_close": 50200.0,
        },
    })
    d2 = build_decision_from_bundle(
        bundle=bundle,
        snap=snap2,
        rolling_features=_rolling_features(ret_1m=-0.002),
    ).model_diagnostics["option_pnl"]
    assert d1["input_hash"] != d2["input_hash"]


def test_all_empty_input_is_visible_in_diagnostics(tmp_path: Path):
    feature_columns = ["px_fut_close", "ret_1m", "opt_flow_pcr_oi"]
    bundle = load_option_pnl_bundle(_write_minimal_bundle(tmp_path, threshold=1.0, feature_columns=feature_columns))
    decision = build_decision_from_bundle(
        bundle=bundle,
        snap=_fake_snap(features={}),
        rolling_features={},
    )
    d = decision.model_diagnostics["option_pnl"]
    assert decision.action == "HOLD"
    assert d["feature_count"] == 3
    assert d["non_null_count"] == 0
    assert d["missing_count"] == 3
    assert d["missing_features"] == feature_columns
