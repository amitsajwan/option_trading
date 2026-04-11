from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.campaign.generator import CampaignGenerator
from ml_pipeline_2.campaign.spec import load_campaign_spec
from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.tests.helpers import build_staged_grid_manifest, build_staged_parquet_root, build_staged_smoke_manifest


def _write_campaign(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _campaign_payload(tmp_path: Path) -> dict:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    return {
        "experiment_kind": "factory_campaign_v1",
        "campaign_id": "campgen",
        "inputs": {"parquet_root": str(parquet_root), "support_dataset": "snapshots_ml_flat"},
        "execution_defaults": {
            "poll_interval_seconds": 0,
            "infra_max_attempts": 2,
            "total_cores": 8,
            "total_memory_gb": 32,
            "ranking_strategy": "publishable_economics_v1",
            "stop_on_first_publishable": False,
            "model_group": "banknifty_futures/h15_tp_auto",
            "profile_id": "openfe_v9_dual",
        },
        "window_profiles": {
            "w1": {
                "research_train": {"start": "2024-01-01", "end": "2024-01-18"},
                "research_valid": {"start": "2024-01-19", "end": "2024-01-24"},
                "full_model": {"start": "2024-01-01", "end": "2024-01-24"},
                "final_holdout": {"start": "2024-01-25", "end": "2024-01-30"},
            }
        },
        "families": {
            "model_families": {
                "m1": {
                    "target": "base_manifest",
                    "models_by_stage": {
                        "stage1": ["logreg_balanced"],
                        "stage2": ["logreg_balanced"],
                        "stage3": ["logreg_balanced"],
                    },
                },
                "m2": {
                    "target": "base_manifest",
                    "models_by_stage": {
                        "stage1": ["logreg_c1"],
                        "stage2": ["logreg_balanced"],
                        "stage3": ["logreg_balanced"],
                    },
                },
            },
            "stage2_policy_families": {
                "p1": {
                    "target": "grid_runs",
                    "run_id_selectors": ["baseline"],
                    "stage2_policy_id": "direction_dual_threshold_v1",
                    "stage2": {"min_edge_grid": [0.01]},
                },
                "p2": {
                    "target": "grid_runs",
                    "run_id_selectors": ["baseline"],
                    "stage2_policy_id": "direction_dual_threshold_v1",
                    "stage2": {"min_edge_grid": [0.05]},
                },
            },
        },
        "lane_templates": [
            {
                "template_id": "upstream",
                "base_grid_path": str(grid_manifest_path),
                "window_profiles": ["w1"],
                "model_families": ["m1", "m2"],
                "stage2_policy_families": ["p1", "p2"],
                "exclude_combinations": [{"model_family": "m2", "stage2_policy_family": "p2"}],
                "max_generated_lanes": 3,
                "resource": {"cores": 2, "memory_gb": 4},
            },
            {
                "template_id": "downstream",
                "base_grid_path": str(grid_manifest_path),
                "window_profiles": ["w1"],
                "model_families": ["m1", "m2"],
                "depends_on_templates": ["upstream"],
                "max_generated_lanes": 2,
                "resource": {"cores": 2, "memory_gb": 4},
            },
        ],
        "campaign_max_lanes": 5,
    }


def _dependency_safe_campaign_payload(tmp_path: Path) -> dict:
    payload = _campaign_payload(tmp_path)
    payload["lane_templates"][1]["stage2_policy_families"] = ["p1"]
    payload["lane_templates"][1]["max_generated_lanes"] = 2
    payload["campaign_max_lanes"] = 5
    return payload


def test_campaign_generator_expands_deterministically_and_writes_legal_manifests(tmp_path: Path) -> None:
    payload = _campaign_payload(tmp_path)
    payload["lane_templates"] = [payload["lane_templates"][0]]
    payload["campaign_max_lanes"] = 3
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", payload))

    expansion = CampaignGenerator(spec, tmp_path / "run").generate()

    lane_ids = [lane.lane_id for lane in expansion.generated_lanes]
    assert lane_ids == [
        "upstream__wp_w1__mf_m1__s2p_p1",
        "upstream__wp_w1__mf_m1__s2p_p2",
        "upstream__wp_w1__mf_m2__s2p_p1",
    ]

    first_lane = expansion.generated_lanes[0]
    staged_payload = json.loads(first_lane.staged_manifest_path.read_text(encoding="utf-8"))
    grid_payload = json.loads(first_lane.grid_manifest_path.read_text(encoding="utf-8"))
    assert staged_payload["outputs"]["run_name"] == first_lane.lane_id
    assert staged_payload["catalog"]["models_by_stage"]["stage1"] == ["logreg_balanced"]
    baseline_run = next(item for item in grid_payload["grid"]["runs"] if item["run_id"] == "baseline")
    assert baseline_run["overrides"]["policy"]["stage2"]["min_edge_grid"] == [0.01]
    assert baseline_run["overrides"]["outputs"]["run_name"] == f"{first_lane.lane_id}__baseline"
    load_and_resolve_manifest(first_lane.staged_manifest_path, validate_paths=True)
    load_and_resolve_manifest(first_lane.grid_manifest_path, validate_paths=True)


def test_campaign_generator_rejects_ambiguous_dependency_mapping(tmp_path: Path) -> None:
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", _campaign_payload(tmp_path)))

    with pytest.raises(ValueError, match="expected exactly 1"):
        CampaignGenerator(spec, tmp_path / "run").generate()


def test_campaign_generator_expands_dependencies_by_matching_shared_axes(tmp_path: Path) -> None:
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", _dependency_safe_campaign_payload(tmp_path)))

    expansion = CampaignGenerator(spec, tmp_path / "run").generate()

    downstream = {lane.lane_id: lane for lane in expansion.generated_lanes if lane.template_id == "downstream"}
    assert downstream["downstream__wp_w1__mf_m1__s2p_p1"].depends_on == ("upstream__wp_w1__mf_m1__s2p_p1",)
    assert downstream["downstream__wp_w1__mf_m2__s2p_p1"].depends_on == ("upstream__wp_w1__mf_m2__s2p_p1",)


def test_campaign_generator_rejects_expansion_above_cap(tmp_path: Path) -> None:
    payload = _campaign_payload(tmp_path)
    payload["lane_templates"][0]["max_generated_lanes"] = 2
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", payload))

    with pytest.raises(ValueError, match="exceeds max_generated_lanes"):
        CampaignGenerator(spec, tmp_path / "run").generate()
