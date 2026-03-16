from __future__ import annotations

from pathlib import Path

import pytest

from market_data_dashboard.research_eval_service import evaluate_recovery_scenario, list_recovery_scenarios
from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


def _build_completed_recovery_run(tmp_path: Path) -> tuple[Path, str]:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    run_dir = Path(summary["output_root"])
    return run_dir, str(summary["selected_primary_recipe_id"])


def test_list_recovery_scenarios_discovers_completed_run(tmp_path: Path) -> None:
    run_dir, recipe_id = _build_completed_recovery_run(tmp_path)

    payload = list_recovery_scenarios(roots=[tmp_path / "artifacts"])

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    scenario = payload["scenarios"][0]
    assert scenario["default_recipe_id"] == recipe_id
    assert scenario["eval_window"]["allowed_start"] == "2024-01-13"
    assert scenario["eval_window"]["allowed_end"] == "2024-01-18"
    assert scenario["scenario_key"] == str(run_dir.resolve())


def test_evaluate_recovery_scenario_returns_chart_and_trade_payload(tmp_path: Path) -> None:
    run_dir, recipe_id = _build_completed_recovery_run(tmp_path)

    payload = evaluate_recovery_scenario(
        scenario_key=str(run_dir.resolve()),
        recipe_id=recipe_id,
        date_from="2024-01-13",
        date_to="2024-01-18",
        roots=[tmp_path / "artifacts"],
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["rows_total"] > 0
    assert payload["summary"]["trades"] > 0
    assert len(payload["chart"]["bars"]) == payload["summary"]["rows_total"]
    assert len(payload["chart"]["entry_markers"]) == payload["summary"]["trades"]
    assert len(payload["chart"]["exit_markers"]) == payload["summary"]["trades"]
    assert payload["trades"][0]["entry_ts"] is not None
    assert payload["trades"][0]["exit_ts"] is not None


def test_evaluate_recovery_scenario_rejects_in_sample_dates(tmp_path: Path) -> None:
    run_dir, recipe_id = _build_completed_recovery_run(tmp_path)

    with pytest.raises(ValueError, match="overlaps the model window"):
        evaluate_recovery_scenario(
            scenario_key=str(run_dir.resolve()),
            recipe_id=recipe_id,
            date_from="2024-01-10",
            date_to="2024-01-14",
            roots=[tmp_path / "artifacts"],
        )
