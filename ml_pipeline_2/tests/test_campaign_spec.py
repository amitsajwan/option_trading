from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.campaign.spec import load_campaign_spec
from ml_pipeline_2.tests.helpers import build_staged_grid_manifest, build_staged_parquet_root, build_staged_smoke_manifest


def _write_campaign(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _base_campaign_payload(tmp_path: Path) -> dict:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    return {
        "experiment_kind": "factory_campaign_v1",
        "campaign_id": "camp1",
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
            "smoke": {
                "research_train": {"start": "2024-01-01", "end": "2024-01-18"},
                "research_valid": {"start": "2024-01-19", "end": "2024-01-24"},
                "full_model": {"start": "2024-01-01", "end": "2024-01-24"},
                "final_holdout": {"start": "2024-01-25", "end": "2024-01-30"},
            }
        },
        "families": {
            "model_families": {
                "logreg_only": {
                    "target": "base_manifest",
                    "models_by_stage": {
                        "stage1": ["logreg_balanced"],
                        "stage2": ["logreg_balanced"],
                        "stage3": ["logreg_balanced"],
                    },
                }
            },
            "stage2_feature_families": {
                "expiry_v3": {
                    "target": "base_manifest",
                    "feature_sets": ["fo_expiry_aware_v3"],
                },
                "grid_feature": {
                    "target": "grid_runs",
                    "run_id_selectors": ["baseline"],
                    "feature_sets": ["fo_expiry_aware_v3"],
                },
            },
            "runtime_families": {
                "expiry_blocked": {
                    "target": "grid_runs",
                    "run_id_selectors": ["best_edge_block_expiry"],
                    "block_expiry": True,
                }
            },
        },
        "lane_templates": [
            {
                "template_id": "t1",
                "base_grid_path": str(grid_manifest_path),
                "window_profiles": ["smoke"],
                "model_families": ["logreg_only"],
                "stage2_feature_families": ["expiry_v3"],
                "runtime_families": ["expiry_blocked"],
                "max_generated_lanes": 2,
                "resource": {"cores": 2, "memory_gb": 4},
            }
        ],
    }


def test_campaign_spec_loads_valid_campaign(tmp_path: Path) -> None:
    campaign_path = _write_campaign(tmp_path / "campaign.json", _base_campaign_payload(tmp_path))

    spec = load_campaign_spec(campaign_path)

    assert spec.campaign_id == "camp1"
    assert spec.lane_templates[0].template_id == "t1"
    assert "logreg_only" in spec.families["model_families"]


def test_campaign_spec_rejects_unknown_family_reference(tmp_path: Path) -> None:
    payload = _base_campaign_payload(tmp_path)
    payload["lane_templates"][0]["stage2_feature_families"] = ["missing_family"]
    campaign_path = _write_campaign(tmp_path / "campaign.json", payload)

    with pytest.raises(ValueError, match="unknown stage2_feature_families"):
        load_campaign_spec(campaign_path)


def test_campaign_spec_rejects_unknown_grid_run_selector(tmp_path: Path) -> None:
    payload = _base_campaign_payload(tmp_path)
    payload["families"]["runtime_families"]["expiry_blocked"]["run_id_selectors"] = ["does_not_exist"]
    campaign_path = _write_campaign(tmp_path / "campaign.json", payload)

    with pytest.raises(ValueError, match="unknown run_id_selectors"):
        load_campaign_spec(campaign_path)


def test_campaign_spec_rejects_invalid_family_payload_shape(tmp_path: Path) -> None:
    payload = _base_campaign_payload(tmp_path)
    payload["families"]["runtime_families"]["expiry_blocked"]["block_expiry"] = "yes"
    campaign_path = _write_campaign(tmp_path / "campaign.json", payload)

    with pytest.raises(ValueError, match="block_expiry must be boolean"):
        load_campaign_spec(campaign_path)
