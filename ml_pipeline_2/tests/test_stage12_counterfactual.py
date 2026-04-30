from __future__ import annotations

from pathlib import Path

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_manifest
from ml_pipeline_2.staged.counterfactual import analyze_stage12_counterfactual
from ml_pipeline_2.tests.helpers import build_staged_parquet_root, build_staged_smoke_manifest


def test_stage12_counterfactual_builds_analysis_from_completed_run(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)

    run_dir = Path(resolved["outputs"]["artifacts_root"]) / "staged_counterfactual_smoke_run"
    summary = run_manifest(
        manifest_path,
        run_output_root=run_dir,
    )

    assert summary["status"] == "completed"

    payload = analyze_stage12_counterfactual(
        run_dir=run_dir,
        top_fractions=(1.0, 0.5),
        fixed_recipe_ids=("L3", "L6"),
    )

    assert payload["analysis_kind"] == "stage12_counterfactual_v1"
    assert payload["source_run_id"] == summary["run_id"]
    assert payload["selected_trade_count"] > 0
    assert payload["fixed_recipe_ids"] == ["L3", "L6"]
    assert payload["recipe_universe_recipe_ids"] == ["L0", "L1", "L2", "L3", "L6"]
    assert len(payload["results"]) == 2
    assert payload["results"][0]["selected_trades"] >= payload["results"][1]["selected_trades"]
    assert (
        payload["results"][0]["oracle_selected_side"]["net_return_sum"]
        >= payload["results"][0]["fixed_recipes"]["L3"]["net_return_sum"]
    )
    assert Path(payload["paths"]["ranked_trades"]).exists()
    assert Path(payload["paths"]["analysis_summary"]).exists()
