from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd


def build_synthetic_feature_frames(root: Path) -> Tuple[Path, Path]:
    seed_root = root / "seed"
    seed_root.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp("2024-01-01")
    rows = []
    total_days = 18
    bars_per_day = 12
    for day_idx in range(total_days):
        trade_day = start + timedelta(days=day_idx)
        direction = 1.0 if (day_idx % 2) == 0 else -1.0
        day_base = 100.0 + (day_idx * 0.5)
        timestamps = pd.date_range(f"{trade_day.date()} 09:15:00", periods=bars_per_day, freq="min")
        max_day = day_base + (direction * 0.25 * (bars_per_day + 2))
        min_day = day_base - (direction * 0.05 * (bars_per_day + 2))
        for bar_idx, ts in enumerate(timestamps):
            open_px = day_base + (direction * 0.25 * bar_idx)
            close_px = open_px + (direction * 0.20)
            if direction > 0:
                high_px = max(open_px, close_px) + 0.18
                low_px = min(open_px, close_px) - 0.03
            else:
                high_px = max(open_px, close_px) + 0.03
                low_px = min(open_px, close_px) - 0.18
            rows.append(
                {
                    "timestamp": ts,
                    "trade_date": str(trade_day.date()),
                    "px_fut_open": open_px,
                    "px_fut_high": high_px,
                    "px_fut_low": low_px,
                    "px_fut_close": close_px,
                    "ret_1m": direction * 0.015,
                    "ret_3m": direction * 0.030,
                    "ema_9_21_spread": direction * 0.75,
                    "osc_rsi_14": 60.0 if direction > 0 else 40.0,
                    "osc_atr_ratio": 0.0015,
                    "vwap_distance": direction * 0.001,
                    "dist_from_day_high": abs(max_day - close_px) / close_px,
                    "dist_from_day_low": abs(close_px - min_day) / close_px,
                    "pcr_oi": 0.95 if direction > 0 else 1.05,
                    "opt_flow_pcr_oi": 0.94 if direction > 0 else 1.06,
                    "dealer_proxy_oi_imbalance": direction * 0.20,
                    "dealer_proxy_oi_imbalance_change_5m": direction * 0.05,
                    "dealer_proxy_pcr_change_5m": direction * -0.03,
                    "dealer_proxy_atm_oi_velocity_5m": direction * 12.0,
                    "dealer_proxy_volume_imbalance": direction * 0.18,
                    "time_minute_of_day": float(ts.hour * 60 + ts.minute),
                    "time_day_of_week": float(ts.dayofweek),
                    "minute_of_day": float(ts.hour * 60 + ts.minute),
                    "day_of_week": float(ts.dayofweek),
                    "ctx_dte_days": float(day_idx % 3),
                    "ctx_is_expiry_day": float((day_idx % 3) == 0),
                    "ctx_is_near_expiry": float((day_idx % 3) <= 1),
                    "ctx_is_high_vix_day": float((day_idx % 4) == 0),
                    "vix_prev_close": 22.0 if (day_idx % 4) == 0 else 16.5,
                    "opt_flow_ce_pe_oi_diff": direction * (100.0 + bar_idx),
                    "fut_flow_oi_change_1m": direction * (10.0 + bar_idx),
                }
            )
    frame = pd.DataFrame(rows)
    model_window = frame[frame["trade_date"] <= "2024-01-12"].copy()
    holdout = frame[frame["trade_date"] >= "2024-01-13"].copy()
    model_window_path = seed_root / "model_window_features.parquet"
    holdout_path = seed_root / "holdout_features.parquet"
    model_window.to_parquet(model_window_path, index=False)
    holdout.to_parquet(holdout_path, index=False)
    return model_window_path, holdout_path


def write_json(path: Path, payload: Dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def build_phase2_smoke_manifest(root: Path, model_window_path: Path, holdout_path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "experiment_kind": "phase2_label_sweep_v1",
        "inputs": {
            "model_window_features_path": str(model_window_path),
            "holdout_features_path": str(holdout_path),
            "base_path": str(root),
        },
        "outputs": {
            "artifacts_root": str(root / "artifacts"),
            "run_name": "phase2_smoke",
        },
        "catalog": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware"],
            "models": ["logreg_balanced"],
        },
        "windows": {
            "research_train": {"start": "2024-01-01", "end": "2024-01-08"},
            "research_valid": {"start": "2024-01-09", "end": "2024-01-12"},
            "full_model": {"start": "2024-01-01", "end": "2024-01-12"},
            "final_holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "training": {
            "objective": "trade_utility",
            "label_target": "path_tp_sl_time_stop_zero",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "utility": {"ce_threshold": 0.50, "pe_threshold": 0.50, "cost_per_trade": 0.0006, "min_profit_factor": 0.50, "max_equity_drawdown_pct": 0.50, "min_trades": 1, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "discard_time_stop": False, "risk_per_trade_pct": 0.01},
        },
        "scenario": {
            "recipes": [
                {"recipe_id": "L1", "horizon_minutes": 2, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005},
                {"recipe_id": "L2", "horizon_minutes": 2, "take_profit_pct": 0.0012, "stop_loss_pct": 0.0006},
            ],
            "threshold_grid": [0.50, 0.55],
            "default_model": "logreg_balanced",
            "stress_models": ["logreg_balanced"],
            "baseline_recipe_ids": ["L1"],
            "acceptance": {"holdout_side_share_min": 0.20, "holdout_side_share_max": 0.80},
            "evaluation_gates": {"long_roc_auc_min": 0.50, "short_roc_auc_min": 0.50, "brier_max": 0.30, "roc_auc_drift_max_abs": 1.0, "futures_pf_min": 0.5, "futures_max_drawdown_pct_max": 1.0, "futures_trades_min": 1, "side_share_min": 0.20, "side_share_max": 0.80, "block_rate_min": 0.0},
        },
    }
    return write_json(root / "phase2_smoke.json", payload)


def build_recovery_smoke_manifest(root: Path, model_window_path: Path, holdout_path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "experiment_kind": "fo_expiry_aware_recovery_v1",
        "inputs": {
            "model_window_features_path": str(model_window_path),
            "holdout_features_path": str(holdout_path),
            "base_path": str(root),
            "baseline_json_path": "",
        },
        "outputs": {
            "artifacts_root": str(root / "artifacts"),
            "run_name": "recovery_smoke",
        },
        "catalog": {
            "feature_profile": "all",
            "feature_sets": ["fo_expiry_aware"],
            "models": ["logreg_balanced"],
        },
        "windows": {
            "full_model": {"start": "2024-01-01", "end": "2024-01-12"},
            "final_holdout": {"start": "2024-01-13", "end": "2024-01-18"},
        },
        "training": {
            "objective": "trade_utility",
            "label_target": "path_tp_sl_resolved_only",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {"train_days": 4, "valid_days": 2, "test_days": 2, "step_days": 2, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "utility": {"ce_threshold": 0.50, "pe_threshold": 0.50, "cost_per_trade": 0.0006, "min_profit_factor": 0.50, "max_equity_drawdown_pct": 0.50, "min_trades": 1, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "discard_time_stop": False, "risk_per_trade_pct": 0.01},
        },
        "scenario": {
            "recipes": [
                {"recipe_id": "TB_BASE_L1", "horizon_minutes": 2, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "barrier_mode": "fixed"},
                {"recipe_id": "TB_ATR_L1", "horizon_minutes": 2, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "barrier_mode": "atr_scaled"},
            ],
            "event_sampling_mode": "none",
            "event_signal_col": "opt_flow_ce_pe_oi_diff",
            "primary_model": "logreg_balanced",
            "primary_threshold": 0.50,
            "meta_gate": {
                "enabled": True,
                "validation_threshold_grid": [0.50]
            },
            "resume_primary": False,
            "recipe_selection": [],
            "evaluation_gates": {"long_roc_auc_min": 0.50, "short_roc_auc_min": 0.50, "brier_max": 0.30, "roc_auc_drift_max_abs": 1.0, "futures_pf_min": 0.5, "futures_max_drawdown_pct_max": 1.0, "futures_trades_min": 1, "side_share_min": 0.20, "side_share_max": 0.80, "block_rate_min": 0.0}
        }
    }
    return write_json(root / "recovery_smoke.json", payload)
