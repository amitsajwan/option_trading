from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from ml_pipeline_2.run_staged_data_preflight import run_staged_data_preflight
from snapshot_app.core.snapshot_ml_flat_contract import REQUIRED_COLUMNS_V2
from snapshot_app.core.velocity_features import VELOCITY_COLUMNS


def _support_frame() -> pd.DataFrame:
    rows = []
    for idx, timestamp in enumerate(("2024-08-01T11:29:00+05:30", "2024-08-01T11:30:00+05:30")):
        row: dict[str, object] = {}
        for column in REQUIRED_COLUMNS_V2:
            if column in {"trade_date"}:
                row[column] = "2024-08-01"
            elif column == "year":
                row[column] = 2024
            elif column == "instrument":
                row[column] = "BANKNIFTY-I"
            elif column == "timestamp":
                row[column] = timestamp
            elif column == "snapshot_id":
                row[column] = f"snap_{idx}"
            elif column == "schema_name":
                row[column] = "SnapshotMLFlatV2"
            elif column == "schema_version":
                row[column] = "4.0"
            elif column == "build_source":
                row[column] = "historical"
            elif column == "build_run_id":
                row[column] = "test_run"
            else:
                row[column] = 1.0
        row["adx_14"] = 24.0
        row["vol_spike_ratio"] = 1.7
        row["ctx_gap_pct"] = 0.002
        row["ctx_gap_up"] = 1
        row["ctx_gap_down"] = 0
        rows.append(row)
    return pd.DataFrame(rows)


def _view_frame() -> pd.DataFrame:
    rows = []
    for idx, timestamp in enumerate(("2024-08-01T11:29:00+05:30", "2024-08-01T11:30:00+05:30")):
        row = {
            "snapshot_id": f"snap_{idx}",
            "instrument": "BANKNIFTY-I",
            "trade_date": "2024-08-01",
            "timestamp": timestamp,
            "schema_name": "SnapshotMLFlatV2",
            "schema_version": "4.0",
            "view_name": "stage_view",
            "build_source": "historical",
            "build_run_id": "test_run",
            "minutes_since_open": 134 + idx,
            "day_of_week": 4,
            "ema_9": 1.0,
            "price_vs_vwap": 0.1,
            "rsi_14_1m": 55.0,
            "atr_14_1m": 12.0,
            "near_atm_oi_ratio": 1.1,
            "atm_oi_ratio": 1.05,
            "vix_current": 14.2,
            "ctx_regime_atr_high": 0,
            "ctx_regime_atr_low": 1,
            "ctx_regime_trend_up": 1,
            "ctx_regime_trend_down": 0,
            "ctx_is_high_vix_day": 0,
            "ctx_dte_days": 2,
            "ctx_is_expiry_day": 0,
            "ctx_is_near_expiry": 1,
            "adx_14": 24.0,
            "vol_spike_ratio": 1.7,
            "ctx_gap_pct": 0.002,
            "ctx_gap_up": 1,
            "ctx_gap_down": 0,
        }
        for column in VELOCITY_COLUMNS:
            row[column] = 1.0
        rows.append(row)
    return pd.DataFrame(rows)


def _write_manifest(parquet_root: Path, manifest_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "experiment_kind": "staged_dual_recipe_v1",
        "inputs": {
            "parquet_root": str(parquet_root),
            "support_dataset": "snapshots_ml_flat_v2",
        },
        "outputs": {
            "artifacts_root": str(parquet_root / "artifacts"),
            "run_name": "velocity_preflight_smoke",
        },
        "catalog": {
            "models_by_stage": {
                "stage1": ["logreg_balanced"],
                "stage2": ["logreg_balanced"],
                "stage3": ["logreg_balanced"],
            },
            "feature_sets_by_stage": {
                "stage1": ["fo_velocity_v1"],
                "stage2": ["fo_velocity_v1"],
                "stage3": ["fo_velocity_v1"],
            },
            "recipe_catalog_id": "fixed_l0_l3_v1",
        },
        "windows": {
            "research_train": {"start": "2024-08-01", "end": "2024-08-01"},
            "research_valid": {"start": "2024-08-02", "end": "2024-08-02"},
            "full_model": {"start": "2024-08-01", "end": "2024-08-02"},
            "final_holdout": {"start": "2024-08-03", "end": "2024-08-03"},
        },
        "views": {
            "stage1_view_id": "stage1_entry_view_v2",
            "stage2_view_id": "stage2_direction_view_v2",
            "stage3_view_id": "stage3_recipe_view_v2",
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
            "cv_config": {"train_days": 1, "valid_days": 1, "test_days": 1, "step_days": 1, "purge_days": 0, "embargo_days": 0, "purge_mode": "days", "embargo_rows": 0, "event_end_col": None},
            "objectives_by_stage": {"stage1": "brier", "stage2": "brier", "stage3": "brier"},
            "random_state": 42,
            "runtime": {"model_n_jobs": 1},
            "cost_per_trade": 0.0006,
        },
        "policy": {
            "stage1_policy_id": "entry_threshold_v1",
            "stage2_policy_id": "direction_dual_threshold_v1",
            "stage3_policy_id": "recipe_top_margin_v1",
            "stage1": {"threshold_grid": [0.45]},
            "stage2": {"ce_threshold_grid": [0.55], "pe_threshold_grid": [0.55], "min_edge_grid": [0.05]},
            "stage3": {"threshold_grid": [0.45], "margin_grid": [0.02]},
        },
        "runtime": {"prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"]},
        "publish": {"publisher_id": "staged_bundle_v1"},
        "hard_gates": {
            "stage1": {"roc_auc_min": 0.55, "brier_max": 0.22, "roc_auc_drift_half_split_max_abs": 0.05},
            "stage2": {"roc_auc_min": 0.55, "brier_max": 0.22},
            "stage3": {"max_drawdown_slack": 0.01},
            "combined": {"profit_factor_min": 1.10, "max_drawdown_pct_max": 0.25, "trades_min": 1, "net_return_sum_min": 0.0, "side_share_min": 0.0, "side_share_max": 1.0, "block_rate_min": 0.0},
        },
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_dataset(dataset_root: Path, frame: pd.DataFrame) -> None:
    year_dir = dataset_root / "year=2024"
    year_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(year_dir / "2024-08-01.parquet", index=False)


def test_staged_data_preflight_passes_for_aligned_v2_support_and_views() -> None:
    tmp_path = Path(tempfile.mkdtemp(prefix="staged-preflight-pass-", dir=Path.cwd()))
    parquet_root = tmp_path / "parquet"
    _write_dataset(parquet_root / "snapshots_ml_flat_v2", _support_frame())
    view_frame = _view_frame()
    _write_dataset(parquet_root / "stage1_entry_view_v2", view_frame)
    _write_dataset(parquet_root / "stage2_direction_view_v2", view_frame)
    _write_dataset(parquet_root / "stage3_recipe_view_v2", view_frame)
    manifest_path = tmp_path / "velocity_manifest.json"
    _write_manifest(parquet_root, manifest_path)

    report = run_staged_data_preflight(manifest_path)

    assert report["status"] == "pass"
    assert report["errors"] == []


def test_staged_data_preflight_fails_on_key_parity_mismatch() -> None:
    tmp_path = Path(tempfile.mkdtemp(prefix="staged-preflight-fail-", dir=Path.cwd()))
    parquet_root = tmp_path / "parquet"
    _write_dataset(parquet_root / "snapshots_ml_flat_v2", _support_frame())
    view_frame = _view_frame()
    _write_dataset(parquet_root / "stage1_entry_view_v2", view_frame.iloc[:1].copy())
    _write_dataset(parquet_root / "stage2_direction_view_v2", view_frame)
    _write_dataset(parquet_root / "stage3_recipe_view_v2", view_frame)
    manifest_path = tmp_path / "velocity_manifest.json"
    _write_manifest(parquet_root, manifest_path)

    report = run_staged_data_preflight(manifest_path)

    assert report["status"] == "fail"
    assert any("key parity failed" in error for error in report["errors"])
