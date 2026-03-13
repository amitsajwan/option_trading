from __future__ import annotations

from pathlib import Path

import pandas as pd

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.run_recovery_threshold_sweep import sweep_recovery_thresholds
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_recovery_threshold_sweep_runs_from_completed_recovery_run(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    run_dir = Path(summary["output_root"])

    sweep = sweep_recovery_thresholds(
        run_dir=run_dir,
        threshold_grid=[0.45, 0.50, 0.55],
    )

    sweep_root = run_dir / "primary_recipes" / str(sweep["recipe_id"]) / "threshold_sweep"
    assert sweep["status"] == "completed"
    assert sweep["recipe_id"] == summary["selected_primary_recipe_id"]
    assert sweep["primary_threshold"] == 0.50
    assert len(list(sweep["rows"])) == 3
    assert Path(str(sweep["paths"]["holdout_labeled"])).exists()
    assert Path(str(sweep["paths"]["holdout_probabilities"])).exists()
    assert Path(str(sweep["paths"]["report_csv"])).exists()
    assert (sweep_root / "summary.json").exists()

    probs = pd.read_parquet(Path(str(sweep["paths"]["holdout_probabilities"])))
    assert {"ce_prob", "pe_prob"} <= set(probs.columns)


def test_recovery_threshold_sweep_can_target_recipe_before_run_summary_selection(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    run_dir = Path(summary["output_root"])

    # Remove the top-level summary to simulate a still-running combo with a completed primary recipe.
    (run_dir / "summary.json").unlink()

    sweep = sweep_recovery_thresholds(
        run_dir=run_dir,
        recipe_id="TB_BASE_L1",
        threshold_grid=[0.50],
    )

    assert sweep["recipe_id"] == "TB_BASE_L1"
    assert len(list(sweep["rows"])) == 1
