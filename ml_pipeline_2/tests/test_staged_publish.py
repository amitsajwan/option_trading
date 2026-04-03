from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

import ml_pipeline_2.publishing.publish as publish_paths
from ml_pipeline_2.publishing import resolve_ml_pure_artifacts, validate_switch_strict
from ml_pipeline_2.experiment_control.runner import ResearchRunFailed
from ml_pipeline_2.staged.publish import publish_staged_run, release_staged_run
from ml_pipeline_2.staged.runtime_contract import load_staged_runtime_policy


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _single_target_package(*, prob: float, model_key: str, prob_col: str) -> dict[str, object]:
    return {
        "feature_columns": ["fut_return_5m", "pcr"],
        "_model_input_contract": {
            "required_features": ["fut_return_5m", "pcr"],
            "allow_extra_features": True,
            "missing_policy": "error",
            "contract_id": "snapshot_stage_view",
        },
        "single_target": {"model_key": model_key, "prob_col": prob_col},
        "models": {model_key: _ConstantProbModel(prob)},
    }


def test_publish_staged_run_writes_runtime_bundle_and_resolver_can_switch(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_publish_fixture_20260318_010101"
    stage1_root = run_dir / "stages" / "stage1"
    stage2_root = run_dir / "stages" / "stage2"
    stage3_root = run_dir / "stages" / "stage3" / "recipes"
    detached_reports_root = run_dir / "stage3_reports"
    stage1_root.mkdir(parents=True, exist_ok=True)
    stage2_root.mkdir(parents=True, exist_ok=True)
    (run_dir / "stages" / "stage3").mkdir(parents=True, exist_ok=True)
    detached_reports_root.mkdir(parents=True, exist_ok=True)
    joblib.dump(_single_target_package(prob=0.8, model_key="move", prob_col="move_prob"), stage1_root / "model.joblib")
    joblib.dump(_single_target_package(prob=0.7, model_key="direction", prob_col="direction_up_prob"), stage2_root / "model.joblib")
    for recipe_id, prob in {"L0": 0.75, "L1": 0.40}.items():
        recipe_root = stage3_root / recipe_id
        recipe_root.mkdir(parents=True, exist_ok=True)
        joblib.dump(_single_target_package(prob=prob, model_key="move", prob_col="move_prob"), recipe_root / "model.joblib")

    summary = {
        "status": "completed",
        "experiment_kind": "staged_dual_recipe_v1",
        "run_id": run_dir.name,
        "recipe_catalog_id": "fixed_l0_l3_v1",
        "publish_assessment": {"decision": "PUBLISH", "publishable": True, "blocking_reasons": []},
        "runtime_prefilter_gate_ids": ["rollout_guard_v1", "feature_freshness_v1"],
        "runtime_block_expiry": True,
        "policy_reports": {
            "stage1": {"policy_id": "entry_threshold_v1", "selected_threshold": 0.55},
            "stage2": {"policy_id": "direction_dual_threshold_v1", "selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
            "stage3": {"policy_id": "recipe_top_margin_v1", "selected_threshold": 0.60, "selected_margin_min": 0.10},
        },
        "component_ids": {
            "stage1": {"view_id": "stage1_entry_view_v1"},
            "stage2": {"view_id": "stage2_direction_view_v1"},
            "stage3": {"view_id": "stage3_recipe_view_v1"},
        },
        "stage_artifacts": {
            "stage1": {"model_package_path": str((stage1_root / "model.joblib").resolve())},
            "stage2": {"model_package_path": str((stage2_root / "model.joblib").resolve())},
            "stage3": {
                "training_report_path": str((detached_reports_root / "training_report.json").resolve()),
                "recipes": ["L0", "L1"],
                "recipe_artifacts": {
                    "L0": {"model_package_path": str((stage3_root / "L0" / "model.joblib").resolve())},
                    "L1": {"model_package_path": str((stage3_root / "L1" / "model.joblib").resolve())},
                },
            },
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    payload = publish_staged_run(
        run_dir=run_dir,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["publish_status"] == "published"
    resolved = resolve_ml_pure_artifacts(run_dir.name, "banknifty_futures/h15_tp_auto")
    ok, reason = validate_switch_strict(dict(resolved["run_report_payload"]))
    assert ok, reason
    threshold_report = json.loads(Path(str(resolved["threshold_report_path"])).read_text(encoding="utf-8"))
    assert threshold_report["kind"] == "ml_pipeline_2_staged_runtime_policy_v1"
    assert threshold_report["runtime"]["block_expiry"] is True
    runtime_bundle = joblib.load(Path(str(resolved["model_package_path"])))
    assert runtime_bundle["runtime"]["block_expiry"] is True
    assert runtime_bundle["stages"]["stage1"]["view_name"] == "stage1_entry_view"
    assert runtime_bundle["stages"]["stage2"]["view_name"] == "stage2_direction_view"
    assert sorted(runtime_bundle["stages"]["stage3"]["recipe_packages"]) == ["L0", "L1"]


def test_release_staged_run_rejects_non_gcs_bucket_url(tmp_path: Path) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_publish_fixture_20260318_010101"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "experiment_kind": "staged_dual_recipe_v1",
                "run_id": run_dir.name,
                "publish_assessment": {"decision": "PUBLISH", "publishable": True, "blocking_reasons": []},
                "recipe_catalog_id": "fixed_l0_l3_v1",
                "runtime_prefilter_gate_ids": [],
                "policy_reports": {"stage1": {}, "stage2": {}, "stage3": {}},
                "component_ids": {
                    "stage1": {"view_id": "stage1_entry_view_v1"},
                    "stage2": {"view_id": "stage2_direction_view_v1"},
                    "stage3": {"view_id": "stage3_recipe_view_v1"},
                },
                "stage_artifacts": {
                    "stage1": {"model_package_path": "missing"},
                    "stage2": {"model_package_path": "missing"},
                    "stage3": {"training_report_path": "missing", "recipes": [], "recipe_artifacts": {}},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="model_bucket_url must start with gs://"):
        release_staged_run(
            run_dir=run_dir,
            model_group="banknifty_futures/h15_tp_auto",
            profile_id="openfe_v9_dual",
            model_bucket_url="C:/tmp/not-a-bucket",
        )


def test_release_staged_run_writes_completed_hold_summary_without_publish(tmp_path: Path) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_hold_fixture_20260321_010101"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "experiment_kind": "staged_dual_recipe_v1",
                "run_id": run_dir.name,
                "publish_assessment": {
                    "decision": "HOLD",
                    "publishable": False,
                    "blocking_reasons": ["stage2.roc_auc<0.55"],
                },
                "completion_mode": "completed",
                "recipe_catalog_id": "fixed_l0_l3_v1",
                "runtime_prefilter_gate_ids": [],
                "policy_reports": {},
                "component_ids": {},
                "stage_artifacts": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = release_staged_run(
        run_dir=run_dir,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["status"] == "completed"
    assert payload["release_status"] == "held"
    assert payload["assessment"]["publishable"] is False
    assert payload["publish"]["publish_status"] == "held"
    assert payload["publish"]["publish_assessment"]["blocking_reasons"] == ["stage2.roc_auc<0.55"]
    assert payload["live_handoff"] is None
    assert payload["gcs_sync"] is None
    assert "runtime_env" not in payload["paths"]
    assert Path(payload["paths"]["assessment"]).exists()
    assert Path(payload["paths"]["release_summary"]).exists()

    written = json.loads(Path(payload["paths"]["release_summary"]).read_text(encoding="utf-8"))
    assert written["release_status"] == "held"
    assert written["publish"]["publish_status"] == "held"
    assert written["paths"]["assessment"].endswith("assessment.json")


def test_release_staged_run_writes_failed_terminal_artifacts_when_research_crashes(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "artifacts" / "research" / "failed_stage_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "run_id": run_dir.name,
                "completion_mode": "failed",
                "error": {
                    "type": "RuntimeError",
                    "message": "synthetic trainer failure",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    def _raise_failed_run(*_args, **_kwargs):
        raise ResearchRunFailed("synthetic trainer failure", output_root=run_dir)

    def _write_json_direct(path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    monkeypatch.setattr("ml_pipeline_2.staged.publish.run_research", _raise_failed_run)
    monkeypatch.setattr("ml_pipeline_2.staged.publish._write_json", _write_json_direct)
    monkeypatch.setattr(
        "ml_pipeline_2.staged.publish.load_and_resolve_manifest",
        lambda *_args, **_kwargs: {
            "publish": {},
        },
    )

    payload = release_staged_run(
        config=tmp_path / "dummy.json",
        run_output_root=run_dir,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["status"] == "failed"
    assert payload["release_status"] == "failed"
    assert payload["assessment"]["publishable"] is False
    assert payload["publish"]["publish_status"] == "failed"
    assert Path(payload["paths"]["assessment"]).exists()
    assert Path(payload["paths"]["release_summary"]).exists()


def test_publish_staged_run_still_rejects_non_publishable_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_hold_fixture_20260321_020202"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "experiment_kind": "staged_dual_recipe_v1",
                "run_id": run_dir.name,
                "publish_assessment": {
                    "decision": "HOLD",
                    "publishable": False,
                    "blocking_reasons": ["combined.trades<50"],
                },
                "recipe_catalog_id": "fixed_l0_l3_v1",
                "policy_reports": {},
                "component_ids": {},
                "stage_artifacts": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="staged run is not publishable"):
        publish_staged_run(
            run_dir=run_dir,
            model_group="banknifty_futures/h15_tp_auto",
            profile_id="openfe_v9_dual",
        )


def test_release_assessment_rejects_contaminated_execution_integrity(tmp_path: Path) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_contaminated_fixture_20260403_010101"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "experiment_kind": "staged_dual_recipe_v1",
                "run_id": run_dir.name,
                "execution_integrity": "contaminated",
                "publish_assessment": {
                    "decision": "PUBLISH",
                    "publishable": True,
                    "blocking_reasons": [],
                },
                "recipe_catalog_id": "fixed_l0_l3_v1",
                "policy_reports": {},
                "component_ids": {},
                "stage_artifacts": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = release_staged_run(
        run_dir=run_dir,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["release_status"] == "held"
    assert payload["assessment"]["publishable"] is False
    assert "execution_integrity=contaminated" in payload["assessment"]["blocking_reasons"]


def test_publish_staged_run_can_force_publish_for_smoke_only(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "ml_pipeline_2" / "artifacts" / "research" / "staged_smoke_hold_fixture_20260324_020202"
    stage1_root = run_dir / "stages" / "stage1"
    stage2_root = run_dir / "stages" / "stage2"
    stage3_root = run_dir / "stages" / "stage3" / "recipes"
    detached_reports_root = run_dir / "stage3_reports"
    stage1_root.mkdir(parents=True, exist_ok=True)
    stage2_root.mkdir(parents=True, exist_ok=True)
    (run_dir / "stages" / "stage3").mkdir(parents=True, exist_ok=True)
    detached_reports_root.mkdir(parents=True, exist_ok=True)
    joblib.dump(_single_target_package(prob=0.8, model_key="move", prob_col="move_prob"), stage1_root / "model.joblib")
    joblib.dump(_single_target_package(prob=0.7, model_key="direction", prob_col="direction_up_prob"), stage2_root / "model.joblib")
    for recipe_id, prob in {"L0": 0.75, "L1": 0.40}.items():
        recipe_root = stage3_root / recipe_id
        recipe_root.mkdir(parents=True, exist_ok=True)
        joblib.dump(_single_target_package(prob=prob, model_key="move", prob_col="move_prob"), recipe_root / "model.joblib")

    summary = {
        "status": "completed",
        "experiment_kind": "staged_dual_recipe_v1",
        "run_id": run_dir.name,
        "recipe_catalog_id": "fixed_l0_l3_v1",
        "publish_assessment": {
            "decision": "HOLD",
            "publishable": False,
            "blocking_reasons": ["stage3.non_inferior_to_fixed_recipe_baseline_failed", "profit_factor<1.0"],
        },
        "runtime_prefilter_gate_ids": ["rollout_guard_v1"],
        "runtime_block_expiry": False,
        "policy_reports": {
            "stage1": {"policy_id": "entry_threshold_v1", "selected_threshold": 0.55},
            "stage2": {"policy_id": "direction_dual_threshold_v1", "selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
            "stage3": {"policy_id": "recipe_top_margin_v1", "selected_threshold": 0.60, "selected_margin_min": 0.10},
        },
        "component_ids": {
            "stage1": {"view_id": "stage1_entry_view_v1"},
            "stage2": {"view_id": "stage2_direction_view_v1"},
            "stage3": {"view_id": "stage3_recipe_view_v1"},
        },
        "stage_artifacts": {
            "stage1": {"model_package_path": str((stage1_root / "model.joblib").resolve())},
            "stage2": {"model_package_path": str((stage2_root / "model.joblib").resolve())},
            "stage3": {
                "training_report_path": str((detached_reports_root / "training_report.json").resolve()),
                "recipes": ["L0", "L1"],
                "recipe_artifacts": {
                    "L0": {"model_package_path": str((stage3_root / "L0" / "model.joblib").resolve())},
                    "L1": {"model_package_path": str((stage3_root / "L1" / "model.joblib").resolve())},
                },
            },
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    payload = publish_staged_run(
        run_dir=run_dir,
        model_group="banknifty_futures/h15_tp_smoke_test",
        profile_id="openfe_v9_dual_smoke",
        force_publish_nonpublishable=True,
    )

    assert payload["publish_status"] == "published"
    assert payload["publish_assessment"]["publishable"] is True


def test_load_staged_runtime_policy_defaults_block_expiry_false(tmp_path: Path) -> None:
    policy_path = tmp_path / "thresholds.json"
    policy_path.write_text(
        json.dumps(
            {
                "kind": "ml_pipeline_2_staged_runtime_policy_v1",
                "stage1": {"selected_threshold": 0.55},
                "stage2": {"selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
                "stage3": {"selected_threshold": 0.60, "selected_margin_min": 0.10},
                "runtime": {"prefilter_gate_ids": ["rollout_guard_v1"]},
                "recipe_catalog": [
                    {"recipe_id": "L0", "horizon_minutes": 15, "take_profit_pct": 0.0025, "stop_loss_pct": 0.0008}
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = load_staged_runtime_policy(policy_path)

    assert payload["runtime"]["block_expiry"] is False


def test_load_staged_runtime_policy_rejects_non_bool_block_expiry(tmp_path: Path) -> None:
    policy_path = tmp_path / "thresholds.json"
    policy_path.write_text(
        json.dumps(
            {
                "kind": "ml_pipeline_2_staged_runtime_policy_v1",
                "stage1": {"selected_threshold": 0.55},
                "stage2": {"selected_ce_threshold": 0.60, "selected_pe_threshold": 0.60, "selected_min_edge": 0.10},
                "stage3": {"selected_threshold": 0.60, "selected_margin_min": 0.10},
                "runtime": {"prefilter_gate_ids": ["rollout_guard_v1"], "block_expiry": "true"},
                "recipe_catalog": [
                    {"recipe_id": "L0", "horizon_minutes": 15, "take_profit_pct": 0.0025, "stop_loss_pct": 0.0008}
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime.block_expiry must be boolean"):
        load_staged_runtime_policy(policy_path)


def test_repo_root_accepts_repo_cwd_without_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ml_pipeline_2").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_SWITCH_REPO_ROOT", raising=False)
    monkeypatch.delenv("ML_PIPELINE_2_REPO_ROOT", raising=False)

    assert publish_paths.repo_root() == tmp_path.resolve()


def test_repo_root_raises_when_no_resolution_path_matches(tmp_path: Path, monkeypatch) -> None:
    fake_install = tmp_path / "venv" / "lib" / "site-packages" / "ml_pipeline_2" / "publishing" / "publish.py"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_SWITCH_REPO_ROOT", raising=False)
    monkeypatch.delenv("ML_PIPELINE_2_REPO_ROOT", raising=False)
    monkeypatch.setattr(publish_paths, "__file__", str(fake_install))

    with pytest.raises(RuntimeError, match="could not resolve option_trading repo root"):
        publish_paths.repo_root()
