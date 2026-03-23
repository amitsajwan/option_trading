from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_manifest
from ml_pipeline_2.model_search import search as search_module
from ml_pipeline_2.staged import pipeline as staged_pipeline
from ml_pipeline_2.tests.helpers import build_staged_parquet_root, build_staged_smoke_manifest


def test_staged_runner_builds_summary_and_stage_artifacts(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_smoke_test_run",
    )

    assert summary["status"] == "completed"
    assert summary["summary_schema_version"] == 3
    assert summary["experiment_kind"] == "staged_dual_recipe_v1"
    assert summary["completion_mode"] == "completed"
    assert sorted(summary["cv_prechecks"]) == ["stage1_cv", "stage2_cv", "stage2_signal_check"]
    assert summary["cv_prechecks"]["stage2_signal_check"]["has_signal"] is True
    assert summary["cv_prechecks"]["stage1_cv"]["gate_passed"] is True
    assert summary["cv_prechecks"]["stage2_cv"]["gate_passed"] is True
    assert isinstance(summary["training_regime_distribution"], dict)
    assert summary["training_regime_distribution"]
    assert summary["policy_reports"]["stage1"]["selected_validation_summary"]["rows_total"] == summary["cv_prechecks"]["stage1_cv"]["rows"]
    assert summary["policy_reports"]["stage2"]["selected_validation_summary"]["rows_total"] == summary["cv_prechecks"]["stage1_cv"]["rows"]
    assert summary["policy_reports"]["stage3"]["selected_validation_summary"]["rows_total"] == summary["cv_prechecks"]["stage1_cv"]["rows"]
    assert summary["holdout_reports"]["stage3"]["combined_holdout_summary"]["rows_total"] == summary["holdout_reports"]["stage1"]["rows"]
    assert summary["component_ids"]["stage1"]["view_id"] == "stage1_entry_view_v1"
    assert summary["component_ids"]["stage2"]["trainer_id"] == "binary_catalog_v1"
    assert summary["component_ids"]["stage3"]["policy_id"] == "recipe_top_margin_v1"
    assert "publish_assessment" in summary
    assert summary["training_environment"]["stage1"]["runnable_models"] == ["logreg_balanced"]
    assert summary["scenario_reports"]["evaluation_mode"] == "combined_policy_holdout"
    assert summary["scenario_reports"]["regime"]["segment_order"] == [
        "TRENDING",
        "SIDEWAYS",
        "VOLATILE",
        "PRE_EXPIRY",
        "UNKNOWN",
    ]
    assert summary["scenario_reports"]["expiry"]["segment_order"] == [
        "EXPIRY_DAY",
        "NEAR_EXPIRY",
        "REGULAR",
    ]
    assert summary["scenario_reports"]["session"]["segment_order"] == [
        "FIRST_HOUR",
        "MID_SESSION",
        "LAST_HOUR",
    ]
    assert summary["scenario_reports"]["expiry"]["segments"]["EXPIRY_DAY"]["rows_total"] >= 0
    assert summary["scenario_reports"]["session"]["segments"]["FIRST_HOUR"]["rows_total"] > 0
    assert summary["scenario_reports"]["regime"]["segments"]["PRE_EXPIRY"]["rows_total"] > 0
    assert sorted(summary["stage_artifacts"]) == ["stage1", "stage2", "stage3"]
    assert summary["stage_artifacts"]["stage1"]["started_at_utc"]
    assert summary["stage_artifacts"]["stage1"]["completed_at_utc"]
    assert Path(summary["stage_artifacts"]["stage1"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage2"]["model_package_path"]).exists()
    assert Path(summary["stage_artifacts"]["stage3"]["training_report_path"]).exists()
    assert sorted(summary["stage_artifacts"]["stage3"]["recipes"]) == ["L0", "L1", "L2", "L3"]


def test_staged_runner_early_holds_when_stage2_signal_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    monkeypatch.setattr(
        staged_pipeline,
        "_check_stage2_signal",
        lambda _frame: {
            "has_signal": False,
            "reason": "max_corr=0.0100<0.05",
            "samples": 180,
            "max_correlation": 0.01,
            "top_features": [{"feature": "pcr_oi", "abs_corr": 0.01}],
        },
    )

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_signal_hold_run",
    )

    assert summary["status"] == "completed"
    assert summary["completion_mode"] == "stage2_signal_check_failed"
    assert summary["publish_assessment"]["decision"] == "HOLD"
    assert summary["publish_assessment"]["publishable"] is False
    assert summary["publish_assessment"]["blocking_reasons"] == ["stage2_signal_check.max_corr=0.0100<0.05"]
    assert summary["stage_artifacts"] == {}
    assert summary["cv_prechecks"]["stage2_signal_check"]["has_signal"] is False
    assert summary["cv_prechecks"]["stage1_cv"] is None
    assert summary["cv_prechecks"]["stage2_cv"] is None
    assert summary["scenario_reports"]["evaluation_mode"] == "coverage_only"
    assert "holdout_reports" not in summary
    assert "policy_reports" not in summary
    assert "gates" not in summary


def test_staged_runner_early_holds_after_stage1_cv_gate_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    original_gate_result = staged_pipeline._stage_gate_result

    def _fake_stage_gate_result(quality: dict[str, object], gates: dict[str, object], *, prefix: str = "") -> tuple[bool, list[str]]:
        if prefix == "stage1_cv.":
            return False, ["stage1_cv.roc_auc<0.99"]
        return original_gate_result(quality, gates, prefix=prefix)

    monkeypatch.setattr(staged_pipeline, "_stage_gate_result", _fake_stage_gate_result)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_stage1_cv_hold_run",
    )

    assert summary["status"] == "completed"
    assert summary["completion_mode"] == "stage1_cv_gate_failed"
    assert sorted(summary["stage_artifacts"]) == ["stage1"]
    assert summary["publish_assessment"]["blocking_reasons"] == ["stage1_cv.roc_auc<0.99"]
    assert summary["cv_prechecks"]["stage1_cv"]["gate_passed"] is False
    assert summary["cv_prechecks"]["stage1_cv"]["reasons"] == ["stage1_cv.roc_auc<0.99"]
    assert summary["cv_prechecks"]["stage2_cv"] is None
    assert "holdout_reports" not in summary


def test_staged_runner_early_holds_after_stage2_cv_gate_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    original_gate_result = staged_pipeline._stage_gate_result

    def _fake_stage_gate_result(quality: dict[str, object], gates: dict[str, object], *, prefix: str = "") -> tuple[bool, list[str]]:
        if prefix == "stage2_cv.":
            return False, ["stage2_cv.brier>0.10"]
        return original_gate_result(quality, gates, prefix=prefix)

    monkeypatch.setattr(staged_pipeline, "_stage_gate_result", _fake_stage_gate_result)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_stage2_cv_hold_run",
    )

    assert summary["status"] == "completed"
    assert summary["completion_mode"] == "stage2_cv_gate_failed"
    assert sorted(summary["stage_artifacts"]) == ["stage1", "stage2"]
    assert summary["publish_assessment"]["blocking_reasons"] == ["stage2_cv.brier>0.10"]
    assert summary["cv_prechecks"]["stage1_cv"]["gate_passed"] is True
    assert summary["cv_prechecks"]["stage2_cv"]["gate_passed"] is False
    assert summary["cv_prechecks"]["stage2_cv"]["reasons"] == ["stage2_cv.brier>0.10"]
    assert "holdout_reports" not in summary


def test_staged_runner_early_holds_when_stage1_cv_metrics_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    original_binary_quality = staged_pipeline._binary_quality
    call_counter = {"count": 0}

    def _fake_binary_quality(labels: pd.Series, probs: pd.Series | pd.DataFrame) -> dict[str, object]:
        call_counter["count"] += 1
        if call_counter["count"] == 1:
            return {
                "rows": int(len(labels)),
                "roc_auc": None,
                "brier": 0.12,
                "roc_auc_drift_half_split": None,
            }
        return original_binary_quality(labels, probs)

    monkeypatch.setattr(staged_pipeline, "_binary_quality", _fake_binary_quality)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_stage1_cv_unavailable_hold_run",
    )

    assert summary["status"] == "completed"
    assert summary["completion_mode"] == "stage1_cv_gate_failed"
    assert summary["publish_assessment"]["blocking_reasons"] == [
        "stage1_cv.roc_auc_unavailable",
        "stage1_cv.roc_auc_drift_unavailable",
    ]
    assert summary["cv_prechecks"]["stage1_cv"]["gate_passed"] is False
    assert summary["cv_prechecks"]["stage1_cv"]["reasons"] == [
        "stage1_cv.roc_auc_unavailable",
        "stage1_cv.roc_auc_drift_unavailable",
    ]
    assert summary["cv_prechecks"]["stage2_cv"] is None


def test_staged_runner_keeps_holdout_rows_total_when_stage3_view_is_missing_holdout_rows(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    stage3_path = parquet_root / "stage3_recipe_view" / "year=2024" / "data.parquet"
    stage3_frame = pd.read_parquet(stage3_path)
    filtered_stage3 = stage3_frame.loc[
        ~(
            (pd.to_datetime(stage3_frame["trade_date"]) >= pd.Timestamp("2024-01-25"))
            & (stage3_frame["snapshot_id"].astype(str).str[-1:].isin({"1", "3", "5", "7", "9"}))
        )
    ].copy()
    filtered_stage3.to_parquet(stage3_path, index=False)
    holdout_stage3_rows = int(
        (
            (pd.to_datetime(filtered_stage3["trade_date"]) >= pd.Timestamp("2024-01-25"))
            & (pd.to_datetime(filtered_stage3["trade_date"]) <= pd.Timestamp("2024-01-30"))
        ).sum()
    )
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_sparse_stage3_holdout_run",
    )

    assert summary["status"] == "completed"
    assert holdout_stage3_rows < summary["holdout_reports"]["stage1"]["rows"]
    assert summary["holdout_reports"]["stage3"]["combined_holdout_summary"]["rows_total"] == summary["holdout_reports"]["stage1"]["rows"]


def test_staged_runner_applies_block_expiry_runtime_filtering_to_training_frames(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["runtime"]["block_expiry"] = True
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_block_expiry_run",
    )

    assert summary["status"] == "completed"
    assert summary["runtime_block_expiry"] is True
    assert summary["runtime_filtering"]["block_expiry"]["enabled"] is True

    support_meta = summary["runtime_filtering"]["block_expiry"]["support"]
    assert support_meta["rows_before"] > support_meta["rows_after"]
    assert support_meta["expiry_rows_dropped"] > 0

    for stage_name in ("stage1", "stage2", "stage3"):
        stage_meta = summary["runtime_filtering"]["block_expiry"]["stages"][stage_name]
        assert stage_meta["rows_before"] > stage_meta["rows_after"]
        assert stage_meta["expiry_rows_dropped"] > 0
        assert stage_meta["signal_column"] in {"ctx_is_expiry_day", "ctx_dte_days"}


def test_staged_runner_validate_only_reports_pruned_training_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["catalog"]["models_by_stage"] = {
        "stage1": ["lgbm_large_v1", "logreg_balanced"],
        "stage2": ["lgbm_large_v1", "logreg_balanced"],
        "stage3": ["lgbm_large_v1", "logreg_balanced"],
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setattr(search_module, "LGBMClassifier", None)

    result = run_manifest(manifest_path, validate_only=True)

    assert result["status"] == "validated"
    assert result["runtime_environment"]["stages"]["stage1"]["requested_models"] == ["lgbm_large_v1", "logreg_balanced"]
    assert result["runtime_environment"]["stages"]["stage1"]["runnable_models"] == ["logreg_balanced"]
    assert result["runtime_environment"]["stages"]["stage1"]["unavailable_models"] == [
        {
            "model_name": "lgbm_large_v1",
            "model_family": "lgbm",
            "missing_dependency": "lightgbm",
            "reason": "requires optional dependency 'lightgbm'",
        }
    ]


def test_staged_runner_supports_stage1_hpo_search_options(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["training"]["search_options_by_stage"] = {
        "stage1": {
            "hpo": {
                "enabled": True,
                "strategy": "random",
                "trials_per_model": 3,
                "sampler_seed": 123,
            }
        }
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_stage1_hpo_run",
    )

    assert summary["status"] == "completed"
    search_report_path = Path(summary["stage_artifacts"]["stage1"]["model_package_path"]).parent / "search_report.json"
    search_report = json.loads(search_report_path.read_text(encoding="utf-8"))
    assert search_report["search_space"]["hpo"] == {
        "enabled": True,
        "strategy": "random",
        "trials_per_model": 3,
        "sampler_seed": 123,
    }
    assert search_report["search_space"]["candidate_models_total"] == 3


def test_staged_runner_supports_stage2_hpo_search_options(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["training"]["search_options_by_stage"] = {
        "stage2": {
            "hpo": {
                "enabled": True,
                "strategy": "random",
                "trials_per_model": 3,
                "sampler_seed": 456,
            }
        }
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    summary = run_manifest(
        manifest_path,
        run_output_root=Path(resolved["outputs"]["artifacts_root"]) / "staged_stage2_hpo_run",
    )

    assert summary["status"] == "completed"
    search_report_path = Path(summary["stage_artifacts"]["stage2"]["model_package_path"]).parent / "search_report.json"
    search_report = json.loads(search_report_path.read_text(encoding="utf-8"))
    assert search_report["search_space"]["hpo"] == {
        "enabled": True,
        "strategy": "random",
        "trials_per_model": 3,
        "sampler_seed": 456,
    }
    assert search_report["search_space"]["candidate_models_total"] == 3


def test_stage2_label_filter_drops_low_edge_rows() -> None:
    stage_frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "timestamp": pd.to_datetime(
                ["2024-01-02 09:16:00", "2024-01-02 09:17:00", "2024-01-02 09:18:00"]
            ),
            "snapshot_id": ["s1", "s2", "s3"],
        }
    )
    oracle = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "timestamp": pd.to_datetime(
                ["2024-01-02 09:16:00", "2024-01-02 09:17:00", "2024-01-02 09:18:00"]
            ),
            "snapshot_id": ["s1", "s2", "s3"],
            "entry_label": [1, 1, 1],
            "direction_label": ["CE", "PE", "CE"],
            "direction_up": [1, 0, 1],
            "recipe_label": ["L0", "L1", "L2"],
            "best_net_return_after_cost": [0.0015, 0.0016, 0.0018],
            "best_ce_net_return_after_cost": [0.0015, 0.0007, 0.0018],
            "best_pe_net_return_after_cost": [0.0009, 0.0016, 0.0006],
            "direction_return_edge_after_cost": [0.0006, 0.0009, 0.0012],
        }
    )

    labeled = staged_pipeline.build_stage2_labels(stage_frame, oracle)
    filtered, meta = staged_pipeline._apply_stage2_label_filter(
        labeled,
        {
            "training": {
                "stage2_label_filter": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.001,
                }
            }
        },
    )

    assert filtered["snapshot_id"].tolist() == ["s3"]
    assert meta["rows_before"] == 3
    assert meta["rows_after"] == 1
    assert meta["rows_dropped"] == 2
    assert meta["min_directional_edge_after_cost"] == 0.001


def test_manifest_accepts_stage2_label_filter(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["training"]["stage2_label_filter"] = {
        "enabled": True,
        "min_directional_edge_after_cost": 0.001,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    assert resolved["training"]["stage2_label_filter"] == {
        "enabled": True,
        "min_directional_edge_after_cost": 0.001,
    }


def test_staged_runner_validate_only_fails_fast_when_stage_has_no_runnable_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["catalog"]["models_by_stage"]["stage1"] = ["lgbm_large_v1"]
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setattr(search_module, "LGBMClassifier", None)

    with pytest.raises(RuntimeError, match="catalog.models_by_stage.stage1"):
        run_manifest(manifest_path, validate_only=True)
