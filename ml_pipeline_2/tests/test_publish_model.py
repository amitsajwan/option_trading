from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_2.publishing import publish_recovery_run
from ml_pipeline_2.publishing import resolve_ml_pure_artifacts, validate_switch_strict
from ml_pipeline_2.run_publish_model import main as publish_main


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _build_completed_recovery_run(root: Path, *, with_threshold_sweep: bool = False) -> tuple[Path, str]:
    run_dir = root / "ml_pipeline_2" / "artifacts" / "research" / "recovery_publish_fixture_20260313_010101"
    recipe_root = run_dir / "primary_recipes" / "FIXED_H15_TP30_SL12"
    recipe_root.mkdir(parents=True, exist_ok=True)

    model_path = recipe_root / "model.joblib"
    training_report_path = recipe_root / "training_report.json"
    package = {
        "feature_columns": ["ret_5m", "opt_flow_pcr_oi", "time_minute_of_day"],
        "_model_input_contract": {
            "required_features": ["ret_5m", "opt_flow_pcr_oi", "time_minute_of_day"],
            "allow_extra_features": True,
            "missing_policy": "error",
            "contract_id": "snapshot_ml_flat_v1",
        },
        "models": {"ce": _ConstantProbModel(0.8), "pe": _ConstantProbModel(0.2)},
    }
    joblib.dump(package, model_path)
    training_report_path.write_text(
        json.dumps(
            {
                "feature_profile": "all",
                "objective": "trade_utility",
                "label_target": "path_tp_sl_resolved_only",
                "trading_utility_config": {
                    "ce_threshold": 0.63,
                    "pe_threshold": 0.61,
                    "cost_per_trade": 0.0006,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    summary = {
        "status": "completed",
        "selected_primary_recipe_id": "FIXED_H15_TP30_SL12",
        "primary_recipes": [
            {
                "recipe": {"recipe_id": "FIXED_H15_TP30_SL12"},
                "model_package_path": str(model_path.resolve()),
                "training_report_path": str(training_report_path.resolve()),
            }
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if with_threshold_sweep:
        sweep_root = recipe_root / "threshold_sweep"
        sweep_root.mkdir(parents=True, exist_ok=True)
        (sweep_root / "summary.json").write_text(
            json.dumps(
                {
                    "recommended_threshold": 0.40,
                    "recommended_row": {
                        "threshold": 0.40,
                        "profit_factor": 1.25,
                        "net_return_sum": 0.12,
                    },
                    "rows": [
                        {"threshold": 0.35, "profit_factor": 1.10, "net_return_sum": 0.08},
                        {"threshold": 0.40, "profit_factor": 1.25, "net_return_sum": 0.12},
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return run_dir, run_dir.name


def test_publish_model_cli_writes_new_published_layout(tmp_path: Path, monkeypatch, capsys) -> None:
    run_dir, run_id = _build_completed_recovery_run(tmp_path)
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    rc = publish_main(
        [
            "--run-dir",
            str(run_dir),
            "--model-group",
            "banknifty_futures/h15_tp_auto",
            "--profile-id",
            "openfe_v9_dual",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["publish_status"] == "published"
    assert payload["publish_decision"]["decision"] == "PUBLISH"

    group_root = tmp_path / "ml_pipeline_2" / "artifacts" / "published_models" / "banknifty_futures" / "h15_tp_auto"
    assert (group_root / "model" / "model.joblib").exists()
    assert (group_root / "config" / "profiles" / "openfe_v9_dual" / "threshold_report.json").exists()
    assert (group_root / "config" / "profiles" / "openfe_v9_dual" / "training_report.json").exists()
    assert (group_root / "model_contract.json").exists()
    assert (group_root / "reports" / "training" / f"run_{run_id}.json").exists()
    assert (group_root / "reports" / "training" / "latest.json").exists()


def test_ml_pipeline_2_publish_resolver_reads_run_specific_artifacts(tmp_path: Path, monkeypatch, capsys) -> None:
    run_dir, run_id = _build_completed_recovery_run(tmp_path)
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    publish_main(
        [
            "--run-dir",
            str(run_dir),
            "--model-group",
            "banknifty_futures/h15_tp_auto",
            "--profile-id",
            "openfe_v9_dual",
        ]
    )
    capsys.readouterr()

    resolved = resolve_ml_pure_artifacts(run_id, "banknifty_futures/h15_tp_auto")
    assert Path(str(resolved["run_report_path"])).exists()
    assert Path(str(resolved["model_package_path"])).exists()
    assert Path(str(resolved["threshold_report_path"])).exists()

    ok, reason = validate_switch_strict(dict(resolved["run_report_payload"]))
    assert ok, reason


def test_publish_model_cli_can_use_threshold_sweep_recommendation(tmp_path: Path, monkeypatch, capsys) -> None:
    run_dir, _ = _build_completed_recovery_run(tmp_path, with_threshold_sweep=True)
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    rc = publish_main(
        [
            "--run-dir",
            str(run_dir),
            "--model-group",
            "banknifty_futures/h15_tp_auto",
            "--profile-id",
            "openfe_v9_dual",
            "--threshold-source",
            "threshold_sweep_recommended",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    threshold_report = json.loads(
        (
            tmp_path
            / "ml_pipeline_2"
            / "artifacts"
            / "published_models"
            / "banknifty_futures"
            / "h15_tp_auto"
            / "config"
            / "profiles"
            / "openfe_v9_dual"
            / "threshold_report.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["threshold_source"] == "threshold_sweep_recommended"
    assert threshold_report["ce_threshold"] == 0.40
    assert threshold_report["pe_threshold"] == 0.40
    assert threshold_report["threshold_source"] == "threshold_sweep_recommended"
    assert threshold_report["threshold_sweep_row"]["threshold"] == 0.40


def test_publish_recovery_run_requires_threshold_sweep_summary_when_requested(tmp_path: Path, monkeypatch) -> None:
    run_dir, _ = _build_completed_recovery_run(tmp_path, with_threshold_sweep=False)
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    try:
        publish_recovery_run(
            run_dir=run_dir,
            model_group="banknifty_futures/h15_tp_auto",
            profile_id="openfe_v9_dual",
            threshold_source="threshold_sweep_recommended",
        )
    except FileNotFoundError as exc:
        assert "threshold sweep summary" in str(exc).lower()
    else:
        raise AssertionError("expected missing threshold sweep summary to fail")
