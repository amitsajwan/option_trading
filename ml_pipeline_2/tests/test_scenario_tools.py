"""Tests for scenario testing tools: scenario_runner, config_diff, results_analyzer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def test_build_manifest_sets_bypass_stage2() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.scenario_runner import build_manifest

    m = build_manifest(bypass_stage2=True, run_name="test_bypass")
    assert m["training"]["bypass_stage2"] is True
    assert m["outputs"]["run_name"] == "test_bypass"


def test_build_manifest_sets_grids() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.scenario_runner import build_manifest

    m = build_manifest(
        stage1_threshold_grid=(0.5, 0.6),
        stage3_threshold_grid=(0.45, 0.55),
        stage3_margin_grid=(0.02,),
    )
    assert m["policy"]["stage1"]["threshold_grid"] == [0.5, 0.6]
    assert m["policy"]["stage3"]["threshold_grid"] == [0.45, 0.55]
    assert m["policy"]["stage3"]["margin_grid"] == [0.02]


def test_scenario_matrix_generates_multiple() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.scenario_runner import scenario_matrix

    manifests = scenario_matrix(
        bypass_stage2_values=(False, True),
        stage1_threshold_values=((0.5,), (0.6,)),
        stage3_threshold_values=((0.45,),),
        stage3_margin_values=((0.02,),),
    )
    assert len(manifests) == 4
    names = [m["outputs"]["run_name"] for m in manifests]
    assert any("bypass" in n for n in names)
    assert any("full" in n for n in names)


def test_diff_manifests_detects_changes() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.config_diff import diff_manifests
    from ml_pipeline_2.staged.scenario_runner import build_manifest

    m1 = build_manifest(bypass_stage2=False, run_name="baseline")
    m2 = build_manifest(bypass_stage2=True, run_name="bypass_test")
    diffs = diff_manifests(m1, m2)
    paths = [d["path"] for d in diffs]
    assert "training.bypass_stage2" in paths
    assert "outputs.run_name" in paths


def test_results_analyzer_on_empty_dict() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.results_analyzer import extract_summary_metrics

    m = extract_summary_metrics({})
    assert m.run_id == ""
    assert m.combined_trades == 0
    assert m.bypass_stage2 is False


def test_results_analyzer_extracts_recipes() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.results_analyzer import extract_summary_metrics

    payload: dict[str, Any] = {
        "policy_reports": {
            "stage3": {
                "validation_rows": [
                    {"selected_recipes": ["L0", "L3"]}
                ]
            }
        }
    }
    m = extract_summary_metrics(payload)
    assert m.selected_recipes == ["L0", "L3"]


def test_run_comparison_markdown() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ml_pipeline_2.staged.results_analyzer import compare_runs, RunMetrics

    d1 = {"run_id": "run_A", "combined_holdout_summary": {"trades": 100, "profit_factor": 1.5}}
    d2 = {"run_id": "run_B", "combined_holdout_summary": {"trades": 200, "profit_factor": 2.0}, "stage_artifacts": {"stage2": {"diagnostics": {"bypass_stage2": True}}}}
    comp = compare_runs([d1, d2])
    md = comp.to_markdown()
    assert "run_A" in md
    assert "run_B" in md
    assert "Y" in md  # bypass_stage2 True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
