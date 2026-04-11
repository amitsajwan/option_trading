from __future__ import annotations

import json
from pathlib import Path

from ml_pipeline_2.campaign.runner import CampaignRunner, resolve_campaign_root
from ml_pipeline_2.campaign.spec import load_campaign_spec
from ml_pipeline_2.factory.state import LaneStatus
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
        "campaign_id": "camprun",
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
                }
            },
        },
        "lane_templates": [
            {
                "template_id": "t1",
                "base_grid_path": str(grid_manifest_path),
                "window_profiles": ["w1"],
                "model_families": ["m1"],
                "max_generated_lanes": 1,
                "resource": {"cores": 2, "memory_gb": 4},
            }
        ],
    }


def test_campaign_runner_generate_only_writes_artifacts(tmp_path: Path) -> None:
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", _campaign_payload(tmp_path)))
    campaign_root = resolve_campaign_root(spec, tmp_path / "out")

    payload = CampaignRunner(spec, campaign_root).run(generate_only=True)

    assert payload["status"] == "generated_only"
    assert (campaign_root / "generated_workflow.json").exists()
    assert (campaign_root / "campaign_result.json").exists()
    assert payload["factory_result"] is None


def test_campaign_runner_wraps_factory_result(monkeypatch, tmp_path: Path) -> None:
    spec = load_campaign_spec(_write_campaign(tmp_path / "campaign.json", _campaign_payload(tmp_path)))
    campaign_root = resolve_campaign_root(spec, tmp_path / "out")

    def fake_run(self):
        lane = self.spec.lanes[0]
        self.state.transition(
            lane.lane_id,
            LaneStatus.RUNNING,
            started_at="2026-01-01T00:00:00Z",
            run_dir=str(self.workflow_root / "lanes" / "01_fake" / "runner_output"),
        )
        self.state.mark_completed(
            lane.lane_id,
            status=LaneStatus.HELD,
            metrics={"profit_factor": 1.1, "net_return_sum": 0.02, "stage2_roc_auc": 0.55},
            summary_path=None,
            error="stage2_cv_gate_failed",
        )
        self.state.completed_at = "2026-01-01T00:00:00Z"
        self.state.save()
        payload = {
            "workflow_id": self.spec.workflow_id,
            "status": "no_publishable_candidate",
            "held_candidates": [lane.lane_id],
            "failed_lanes": [],
            "lane_summary": [{"lane_id": lane.lane_id, "status": "held", "attempts": 1}],
        }
        (self.workflow_root / "workflow_result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    monkeypatch.setattr("ml_pipeline_2.campaign.runner.WorkflowRunner.run", fake_run)

    payload = CampaignRunner(spec, campaign_root).run(generate_only=False)

    assert payload["status"] == "no_publishable_candidate"
    assert payload["factory_result"]["status"] == "no_publishable_candidate"
    assert payload["best_nonpublishable_by_template"][0]["template_id"] == "t1"
    assert payload["blocking_reasons_by_family"]["window_profile"][0]["value"] == "w1"
