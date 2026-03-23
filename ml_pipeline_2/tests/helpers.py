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


def _staged_rel_path(pattern: str, step_idx: int) -> float:
    if pattern == "ce_l0":
        return 0.00035 * step_idx if step_idx <= 8 else 0.0028
    if pattern == "pe_l0":
        return -0.00035 * step_idx if step_idx <= 8 else -0.0028
    if pattern == "ce_l1":
        if step_idx <= 5:
            return 0.00042 * step_idx
        if step_idx <= 15:
            return 0.0021 - 0.00016 * (step_idx - 5)
        return 0.0005
    if pattern == "pe_l1":
        if step_idx <= 5:
            return -0.00042 * step_idx
        if step_idx <= 15:
            return -0.0021 + 0.00016 * (step_idx - 5)
        return -0.0005
    if pattern == "ce_l2":
        if step_idx <= 2:
            return -0.00045 * step_idx
        if step_idx <= 8:
            return -0.0009 + 0.00052 * (step_idx - 2)
        if step_idx <= 15:
            return 0.00222 - 0.00018 * (step_idx - 8)
        return 0.0010
    if pattern == "pe_l2":
        if step_idx <= 2:
            return 0.00045 * step_idx
        if step_idx <= 8:
            return 0.0009 - 0.00052 * (step_idx - 2)
        if step_idx <= 15:
            return -0.00222 + 0.00018 * (step_idx - 8)
        return -0.0010
    if pattern == "ce_l3":
        return 0.000145 * step_idx if step_idx <= 18 else 0.00265
    if pattern == "pe_l3":
        return -0.000145 * step_idx if step_idx <= 18 else -0.00265
    if pattern == "flat_a":
        return 0.00003 * ((step_idx % 4) - 1.5)
    if pattern == "flat_b":
        return -0.00003 * ((step_idx % 5) - 2)
    raise ValueError(f"unknown staged pattern: {pattern}")


def build_staged_parquet_root(root: Path) -> Path:
    parquet_root = root / "parquet"
    parquet_root.mkdir(parents=True, exist_ok=True)

    patterns = (
        "ce_l0",
        "pe_l0",
        "ce_l1",
        "pe_l1",
        "ce_l2",
        "pe_l2",
        "ce_l3",
        "pe_l3",
        "flat_a",
        "flat_b",
    )
    bars_per_day = 45
    trade_days = pd.date_range("2024-01-01", periods=30, freq="D")
    rows = []
    for day_idx, trade_day in enumerate(trade_days):
        pattern = str(patterns[day_idx % len(patterns)])
        base = 100000.0 + (50.0 * day_idx)
        timestamps = pd.date_range(f"{trade_day.date()} 09:15:00", periods=bars_per_day, freq="min")
        direction = 1.0 if pattern.startswith("ce") else (-1.0 if pattern.startswith("pe") else 0.0)
        rel_path = [_staged_rel_path(pattern, step_idx) for step_idx in range(bars_per_day + 1)]
        for bar_idx, ts in enumerate(timestamps):
            open_px = base * (1.0 + rel_path[bar_idx])
            close_px = base * (1.0 + rel_path[bar_idx + 1])
            wiggle = 0.00015 if "flat" not in pattern else 0.00003
            high_px = max(open_px, close_px) * (1.0 + wiggle)
            low_px = min(open_px, close_px) * (1.0 - wiggle)
            ret_1m = (close_px - open_px) / open_px
            rows.append(
                {
                    "trade_date": str(trade_day.date()),
                    "timestamp": ts,
                    "snapshot_id": f"{trade_day.strftime('%Y%m%d')}_{bar_idx:04d}",
                    "px_fut_open": open_px,
                    "px_fut_high": high_px,
                    "px_fut_low": low_px,
                    "px_fut_close": close_px,
                    "ret_1m": ret_1m,
                    "ret_3m": direction * 0.0009,
                    "ret_5m": direction * 0.0014,
                    "ema_9_21_spread": direction * 0.6,
                    "ema_9_slope": direction * 0.15,
                    "ema_21_slope": direction * 0.10,
                    "ema_50_slope": direction * 0.08,
                    "osc_rsi_14": 62.0 if direction > 0 else (38.0 if direction < 0 else 50.0),
                    "osc_atr_ratio": 0.0015 if "flat" not in pattern else 0.0004,
                    "osc_atr_daily_percentile": 0.70 if "flat" not in pattern else 0.20,
                    "vwap_distance": direction * 0.0008,
                    "dist_from_day_high": max(0.0, (high_px - close_px) / close_px),
                    "dist_from_day_low": max(0.0, (close_px - low_px) / close_px),
                    "pcr_oi": 0.92 if direction > 0 else (1.08 if direction < 0 else 1.0),
                    "opt_flow_pcr_oi": 0.93 if direction > 0 else (1.07 if direction < 0 else 1.0),
                    "dealer_proxy_oi_imbalance": direction * 0.22,
                    "dealer_proxy_oi_imbalance_change_5m": direction * 0.05,
                    "dealer_proxy_pcr_change_5m": -direction * 0.03,
                    "dealer_proxy_atm_oi_velocity_5m": direction * 10.0,
                    "dealer_proxy_volume_imbalance": direction * 0.16,
                    "time_minute_of_day": float(ts.hour * 60 + ts.minute),
                    "time_day_of_week": float(ts.dayofweek),
                    "minute_of_day": float(ts.hour * 60 + ts.minute),
                    "day_of_week": float(ts.dayofweek),
                    "ctx_dte_days": float(day_idx % 4),
                    "ctx_is_expiry_day": float((day_idx % 7) == 0),
                    "ctx_is_near_expiry": float((day_idx % 4) <= 1),
                    "ctx_regime_atr_high": float("flat" not in pattern),
                    "ctx_regime_atr_low": float("flat" in pattern),
                    "ctx_regime_trend_up": float(direction > 0),
                    "ctx_regime_trend_down": float(direction < 0),
                    "ctx_regime_expiry_near": float((day_idx % 4) <= 1),
                    "ctx_is_high_vix_day": float((day_idx % 6) == 0),
                    "regime_vol_high": float("flat" not in pattern),
                    "regime_atr_high": float("flat" not in pattern),
                    "regime_atr_low": float("flat" in pattern),
                    "regime_trend_up": float(direction > 0),
                    "regime_trend_down": float(direction < 0),
                    "regime_expiry_near": float((day_idx % 4) <= 1),
                    "stage_pattern_code": float(day_idx % len(patterns)),
                }
            )
    frame = pd.DataFrame(rows)
    for dataset_name in ("snapshots_ml_flat", "stage1_entry_view", "stage2_direction_view", "stage3_recipe_view"):
        out_dir = parquet_root / dataset_name / "year=2024"
        out_dir.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(out_dir / "data.parquet", index=False)
    return parquet_root


def build_staged_smoke_manifest(root: Path, parquet_root: Path) -> Path:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {
            "parquet_root": str(parquet_root),
            "support_dataset": "snapshots_ml_flat",
        },
        "outputs": {
            "artifacts_root": str(root / "artifacts"),
            "run_name": "staged_smoke",
        },
        "catalog": {
            "models_by_stage": {
                "stage1": ["logreg_balanced"],
                "stage2": ["logreg_balanced"],
                "stage3": ["logreg_balanced"],
            },
            "feature_sets_by_stage": {
                "stage1": ["fo_expiry_aware_v2"],
                "stage2": ["fo_expiry_aware_v3"],
                "stage3": ["fo_full"],
            },
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2024-01-01", "end": "2024-01-18"},
            "research_valid": {"start": "2024-01-19", "end": "2024-01-24"},
            "full_model": {"start": "2024-01-01", "end": "2024-01-24"},
            "final_holdout": {"start": "2024-01-25", "end": "2024-01-30"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v1",
            "stage2_view_id": "stage2_direction_view_v1",
            "stage3_view_id": "stage3_recipe_view_v1",
        },
        "labels": {
            "stage1_labeler_id": "entry_best_recipe_v1",
            "stage2_labeler_id": "direction_best_recipe_v1",
            "stage3_labeler_id": "recipe_best_positive_v1",
        },
        "training": {
            "stage1_trainer_id": "binary_catalog_v1",
            "stage2_trainer_id": "binary_catalog_v1",
            "stage3_trainer_id": "ovr_recipe_catalog_v1",
            "preprocess": {"max_missing_rate": 0.35, "clip_lower_q": 0.01, "clip_upper_q": 0.99},
            "cv_config": {
                "train_days": 8,
                "valid_days": 4,
                "test_days": 4,
                "step_days": 4,
                "purge_days": 0,
                "embargo_days": 0,
                "purge_mode": "days",
                "embargo_rows": 0,
                "event_end_col": None,
            },
            "objectives_by_stage": {
                "stage1": "brier",
                "stage2": "brier",
                "stage3": "brier",
            },
            "random_state": 42,
            "runtime": {"model_n_jobs": 1},
            "cost_per_trade": 0.0001,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45, 0.50, 0.55]},
            "stage2": {
                "ce_threshold_grid": [0.45, 0.50, 0.55],
                "pe_threshold_grid": [0.45, 0.50, 0.55],
                "min_edge_grid": [0.01, 0.05],
            },
            "stage3": {"threshold_grid": [0.45, 0.50], "margin_grid": [0.01, 0.05]},
        },
        "runtime": {
            "prefilter_gate_ids": [
                "rollout_guard_v1",
                "feature_freshness_v1",
                "feature_completeness_v1",
                "liquidity_gate_v1",
                "regime_gate_v1",
                "regime_confidence_gate_v1",
            ]
        },
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {
                "roc_auc_min": 0.50,
                "brier_max": 0.40,
                "roc_auc_drift_half_split_max_abs": 1.0,
            },
            "stage2": {
                "roc_auc_min": 0.50,
                "brier_max": 0.40,
            },
            "stage3": {"max_drawdown_slack": 1.0},
            "combined": {
                "profit_factor_min": 1.0,
                "max_drawdown_pct_max": 1.0,
                "trades_min": 1,
                "net_return_sum_min": -1.0,
                "side_share_min": 0.0,
                "side_share_max": 1.0,
                "block_rate_min": 0.0,
            },
        },
    }
    return write_json(root / "staged_smoke.json", payload)


def build_staged_grid_manifest(root: Path, base_manifest_path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_training_grid_v1",
        "inputs": {
            "base_manifest_path": str(base_manifest_path),
        },
        "outputs": {
            "artifacts_root": str(root / "grid_artifacts"),
            "run_name": "staged_grid_smoke",
        },
        "selection": {
            "stage2_hpo_escalation": {
                "roc_auc_min": 0.54,
                "brier_max": 0.225,
            }
        },
        "grid": {
            "research_only": True,
            "max_parallel_runs": 2,
            "runs": [
                {
                    "run_id": "baseline",
                    "model_group_suffix": "_baseline",
                    "overrides": {
                        "outputs": {
                            "run_name": "staged_grid_baseline",
                        }
                    },
                },
                {
                    "run_id": "edge_0006",
                    "model_group_suffix": "_edge_0006",
                    "overrides": {
                        "outputs": {
                            "run_name": "staged_grid_edge_0006",
                        },
                        "training": {
                            "stage2_label_filter": {
                                "enabled": True,
                                "min_directional_edge_after_cost": 0.0006,
                            }
                        },
                    },
                },
                {
                    "run_id": "edge_0010",
                    "model_group_suffix": "_edge_0010",
                    "overrides": {
                        "outputs": {
                            "run_name": "staged_grid_edge_0010",
                        },
                        "training": {
                            "stage2_label_filter": {
                                "enabled": True,
                                "min_directional_edge_after_cost": 0.001,
                            }
                        },
                    },
                },
                {
                    "run_id": "best_edge_block_expiry",
                    "model_group_suffix": "_best_edge_block_expiry",
                    "inherit_best_from": ["edge_0006", "edge_0010"],
                    "overrides": {
                        "outputs": {
                            "run_name": "staged_grid_best_edge_block_expiry",
                        },
                        "runtime": {
                            "block_expiry": True,
                        },
                    },
                },
            ],
        },
    }
    return write_json(root / "staged_grid_smoke.json", payload)
