from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.coordination import CoordinationError
from ml_pipeline_2.staged import grid as grid_module
from ml_pipeline_2.tests.helpers import (
    build_staged_grid_manifest,
    build_staged_parquet_root,
    build_staged_smoke_manifest,
)


def _mock_summary(
    *,
    run_name: str,
    publishable: bool,
    stage2_auc: float,
    stage2_brier: float,
    profit_factor: float,
    net_return_sum: float,
    max_drawdown_pct: float,
) -> dict[str, object]:
    blocking_reasons = [] if publishable else ["stage2_cv.roc_auc<0.55", "stage2_cv.brier>0.22"]
    return {
        "status": "completed",
        "run_id": run_name,
        "completion_mode": "completed",
        "cv_prechecks": {
            "stage2_signal_check": {"has_signal": True, "max_correlation": 0.06, "top_features": []},
            "stage1_cv": {
                "rows": 100,
                "roc_auc": 0.61,
                "brier": 0.20,
                "roc_auc_first_half": 0.60,
                "roc_auc_second_half": 0.62,
                "roc_auc_drift_half_split": 0.02,
                "gate_passed": True,
                "reasons": [],
            },
            "stage2_cv": {
                "rows": 80,
                "roc_auc": stage2_auc,
                "brier": stage2_brier,
                "roc_auc_first_half": stage2_auc,
                "roc_auc_second_half": stage2_auc,
                "roc_auc_drift_half_split": 0.01,
                "gate_passed": publishable,
                "reasons": [] if publishable else ["stage2_cv.roc_auc<0.55", "stage2_cv.brier>0.22"],
            },
        },
        "holdout_reports": {
            "stage3": {
                "combined_holdout_summary": {
                    "rows_total": 120,
                    "trades": 60,
                    "block_rate": 0.5,
                    "net_return_sum": net_return_sum,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "win_rate": 0.55,
                    "long_share": 0.5,
                    "short_share": 0.5,
                    "side_share_in_band": True,
                    "selected_recipes": ["L0", "L1"],
                }
            }
        },
        "publish_assessment": {
            "decision": "PUBLISH" if publishable else "HOLD",
            "publishable": publishable,
            "blocking_reasons": blocking_reasons,
        },
        "scenario_reports": {
            "evaluation_mode": "combined_policy_holdout",
            "regime": {"segment_order": ["TRENDING"], "segments": {"TRENDING": {"rows_total": 10, "trades": 5}}},
            "expiry": {"segment_order": ["REGULAR"], "segments": {"REGULAR": {"rows_total": 10, "trades": 5}}},
            "session": {"segment_order": ["MID_SESSION"], "segments": {"MID_SESSION": {"rows_total": 10, "trades": 5}}},
        },
    }


def test_staged_grid_runner_ranks_runs_and_keeps_execution_research_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

    summaries_by_run_name = {
        "staged_grid_baseline": _mock_summary(
            run_name="staged_grid_baseline",
            publishable=False,
            stage2_auc=0.520,
            stage2_brier=0.250,
            profit_factor=1.05,
            net_return_sum=0.01,
            max_drawdown_pct=0.09,
        ),
        "staged_grid_edge_0006": _mock_summary(
            run_name="staged_grid_edge_0006",
            publishable=False,
            stage2_auc=0.533,
            stage2_brier=0.231,
            profit_factor=1.12,
            net_return_sum=0.03,
            max_drawdown_pct=0.08,
        ),
        "staged_grid_edge_0010": _mock_summary(
            run_name="staged_grid_edge_0010",
            publishable=False,
            stage2_auc=0.541,
            stage2_brier=0.224,
            profit_factor=1.18,
            net_return_sum=0.05,
            max_drawdown_pct=0.07,
        ),
        "staged_grid_best_edge_block_expiry": _mock_summary(
            run_name="staged_grid_best_edge_block_expiry",
            publishable=True,
            stage2_auc=0.559,
            stage2_brier=0.218,
            profit_factor=1.35,
            net_return_sum=0.11,
            max_drawdown_pct=0.05,
        ),
    }

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        run_name = str(resolved_config["outputs"]["run_name"])
        summary = dict(summaries_by_run_name[run_name])
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)

    payload = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert payload["status"] == "completed"
    assert payload["research_only"] is True
    assert payload["execution"]["max_parallel_runs"] == 2
    assert payload["execution"]["base_model_n_jobs"] == 1
    assert payload["winner"]["grid_run_id"] == "best_edge_block_expiry"
    assert payload["winner_release"] is None
    assert payload["orchestration_integrity"] == "clean"
    assert payload["stage2_hpo_escalation"]["eligible"] is True
    assert payload["stage2_hpo_escalation"]["best_run_id"] == "best_edge_block_expiry"
    assert Path(payload["paths"]["grid_summary"]).exists()
    grid_status = json.loads(Path(payload["paths"]["grid_status"]).read_text(encoding="utf-8"))
    assert grid_status["status"] == "completed"
    assert grid_status["integrity"] == "clean"

    run_rows = {row["grid_run_id"]: row for row in payload["runs"]}
    assert run_rows["best_edge_block_expiry"]["rank"] == 1
    assert run_rows["edge_0010"]["rank"] < run_rows["edge_0006"]["rank"]

    inherited_manifest_path = Path(run_rows["best_edge_block_expiry"]["manifest_path"])
    inherited_manifest = json.loads(inherited_manifest_path.read_text(encoding="utf-8"))
    assert inherited_manifest["training"]["stage2_label_filter"]["min_directional_edge_after_cost"] == 0.001
    assert inherited_manifest["runtime"]["block_expiry"] is True

    assert not any((Path(row["run_dir"]) / "release").exists() for row in payload["runs"])


def test_staged_grid_runner_writes_time_focus_override_from_grid_catalog(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = tmp_path / "staged_grid_time_focus.json"
    grid_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment_kind": "staged_training_grid_v1",
                "inputs": {"base_manifest_path": str(base_manifest_path)},
                "outputs": {"artifacts_root": str(tmp_path / "grid_artifacts"), "run_name": "staged_grid_time_focus"},
                "selection": {"stage2_hpo_escalation": {"roc_auc_min": 0.54, "brier_max": 0.225}},
                "grid": {
                    "research_only": True,
                    "runs": [
                        {
                            "run_id": "edge_0006",
                            "model_group_suffix": "_edge_0006",
                            "overrides": {
                                "outputs": {"run_name": "grid_edge_0006"},
                                "training": {
                                    "stage2_label_filter": {
                                        "enabled": True,
                                        "min_directional_edge_after_cost": 0.0006,
                                    }
                                },
                            },
                        },
                        {
                            "run_id": "edge_0010",
                            "model_group_suffix": "_edge_0010",
                            "overrides": {
                                "outputs": {"run_name": "grid_edge_0010"},
                                "training": {
                                    "stage2_label_filter": {
                                        "enabled": True,
                                        "min_directional_edge_after_cost": 0.001,
                                    }
                                },
                            },
                        },
                        {
                            "run_id": "best_edge_time_focus",
                            "model_group_suffix": "_best_edge_time_focus",
                            "inherit_best_from": ["edge_0006", "edge_0010"],
                            "overrides": {
                                "outputs": {"run_name": "grid_best_edge_time_focus"},
                                "catalog": {
                                    "feature_sets_by_stage": {
                                        "stage2": ["fo_expiry_aware_v3", "fo_no_time_context", "fo_no_opening_range"]
                                    }
                                },
                            },
                        },
                    ],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        summary = _mock_summary(
            run_name=str(resolved_config["outputs"]["run_name"]),
            publishable=False,
            stage2_auc=0.54 if str(resolved_config["outputs"]["run_name"]) == "grid_edge_0010" else 0.53,
            stage2_brier=0.224 if str(resolved_config["outputs"]["run_name"]) == "grid_edge_0010" else 0.231,
            profit_factor=1.1,
            net_return_sum=0.02,
            max_drawdown_pct=0.08,
        )
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)

    payload = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    run_rows = {row["grid_run_id"]: row for row in payload["runs"]}
    assert payload["execution"]["max_parallel_runs"] >= 1
    manifest_payload = json.loads(Path(run_rows["best_edge_time_focus"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest_payload["training"]["stage2_label_filter"]["min_directional_edge_after_cost"] == 0.001
    assert manifest_payload["catalog"]["feature_sets_by_stage"]["stage2"] == [
        "fo_expiry_aware_v3",
        "fo_no_time_context",
        "fo_no_opening_range",
    ]


def test_research_only_grid_rejects_publish_winner(tmp_path: Path) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

    with pytest.raises(ValueError, match="grid manifest is research_only"):
        grid_module.run_staged_grid(
            resolved,
            model_group="banknifty_futures/h15_tp_auto",
            profile_id="openfe_v9_dual",
            publish_winner=True,
        )


def test_staged_grid_runner_attaches_stage2_robustness_probe(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    payload = json.loads(grid_manifest_path.read_text(encoding="utf-8"))
    payload["selection"]["robustness_probe"] = {
        "enabled": True,
        "top_k": 1,
        "iterations": 25,
        "random_seed": 9,
        "splits": ["research_valid", "final_holdout"],
        "resample_unit": "trade_date",
    }
    grid_manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        run_name = str(resolved_config["outputs"]["run_name"])
        auc_map = {
            "staged_grid_baseline": 0.520,
            "staged_grid_edge_0006": 0.533,
            "staged_grid_edge_0010": 0.541,
            "staged_grid_best_edge_block_expiry": 0.530,
        }
        brier_map = {
            "staged_grid_baseline": 0.250,
            "staged_grid_edge_0006": 0.231,
            "staged_grid_edge_0010": 0.224,
            "staged_grid_best_edge_block_expiry": 0.232,
        }
        summary = _mock_summary(
            run_name=run_name,
            publishable=False,
            stage2_auc=auc_map[run_name],
            stage2_brier=brier_map[run_name],
            profit_factor=1.1,
            net_return_sum=0.02,
            max_drawdown_pct=0.08,
        )
        summary["stage_artifacts"] = {
            "stage2": {
                "diagnostics_score_paths": {
                    "research_valid": str((output_root / "research_valid.parquet").resolve()),
                    "final_holdout": str((output_root / "final_holdout.parquet").resolve()),
                }
            }
        }
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)
    monkeypatch.setattr(
        grid_module,
        "bootstrap_stage2_scores_from_parquet",
        lambda *_args, **_kwargs: {
            "resample_unit": "trade_date",
            "iterations": 25,
            "units_total": 10,
            "rows_total": 100,
            "base_quality": {"roc_auc": 0.56, "brier": 0.24},
            "bootstrap_metrics": {
                "roc_auc": {"count": 25, "mean": 0.55, "std": 0.01, "min": 0.53, "p05": 0.535, "p50": 0.55, "p95": 0.565, "max": 0.57},
                "brier": {"count": 25, "mean": 0.24, "std": 0.004, "min": 0.232, "p05": 0.234, "p50": 0.24, "p95": 0.246, "max": 0.248},
            },
            "gate_pass_rate": 0.24,
        },
    )

    result = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    assert result["robustness_probe"]["enabled"] is True
    assert result["robustness_probe"]["evaluated_run_ids"] == ["edge_0010"]
    run_rows = {row["grid_run_id"]: row for row in result["runs"]}
    assert run_rows["edge_0010"]["stage2_robustness"]["splits"]["research_valid"]["status"] == "computed"
    assert run_rows["edge_0010"]["stage2_robustness"]["splits"]["research_valid"]["gate_pass_rate"] == 0.24


def test_staged_grid_runner_passes_stage1_reuse_execution_hint(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = tmp_path / "staged_grid_stage1_reuse.json"
    grid_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                    "experiment_kind": "staged_training_grid_v1",
                    "inputs": {"base_manifest_path": str(base_manifest_path)},
                    "outputs": {"artifacts_root": str(tmp_path / "grid_artifacts"), "run_name": "staged_grid_stage1_reuse"},
                    "selection": {"stage2_hpo_escalation": {"roc_auc_min": 0.54, "brier_max": 0.225}},
                    "grid": {
                    "research_only": True,
                    "max_parallel_runs": 1,
                    "runs": [
                        {
                            "run_id": "baseline",
                            "model_group_suffix": "_baseline",
                            "overrides": {"outputs": {"run_name": "grid_reuse_baseline"}},
                        },
                        {
                            "run_id": "midday_variant",
                            "model_group_suffix": "_midday_variant",
                            "reuse_stage1_from": "baseline",
                            "overrides": {
                                "outputs": {"run_name": "grid_reuse_midday_variant"},
                                "training": {
                                    "stage2_session_filter": {
                                        "enabled": True,
                                        "include_buckets": ["MIDDAY"],
                                    }
                                },
                            },
                        },
                    ],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)
    seen_hints: dict[str, object] = {}

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        run_name = str(resolved_config["outputs"]["run_name"])
        if run_name == "grid_reuse_midday_variant":
            seen_hints["value"] = dict(resolved_config.get("_execution_hints") or {})
        summary = _mock_summary(
            run_name=run_name,
            publishable=False,
            stage2_auc=0.53,
            stage2_brier=0.24,
            profit_factor=1.1,
            net_return_sum=0.02,
            max_drawdown_pct=0.08,
        )
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)

    grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    hint = dict(seen_hints["value"])["stage1_reuse"]
    assert hint["source_run_id"] == "baseline"
    assert str(hint["source_run_dir"]).endswith("01_baseline")
    assert str(hint["source_summary_path"]).endswith("01_baseline\\summary.json") or str(hint["source_summary_path"]).endswith("01_baseline/summary.json")


def test_grid_dependency_inheritance_requires_successful_prior_runs(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)

    def _always_fail_run_research(_resolved_config, *, run_output_root=None):
        raise RuntimeError("synthetic training failure")

    monkeypatch.setattr(grid_module, "run_research", _always_fail_run_research)

    payload = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
    )

    run_rows = {row["grid_run_id"]: row for row in payload["runs"]}
    assert run_rows["baseline"]["release_status"] == "failed"
    assert run_rows["edge_0006"]["release_status"] == "failed"
    assert run_rows["edge_0010"]["release_status"] == "failed"
    assert run_rows["best_edge_block_expiry"]["release_status"] == "failed"
    assert "no successful prior runs" in str(run_rows["best_edge_block_expiry"]["blocking_reasons"][0])


def test_staged_grid_fail_if_exists_blocks_reuse_of_existing_grid_root(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)
    grid_root = tmp_path / "grid_root_blocked"

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        summary = _mock_summary(
            run_name=str(resolved_config["outputs"]["run_name"]),
            publishable=False,
            stage2_auc=0.52,
            stage2_brier=0.25,
            profit_factor=1.05,
            net_return_sum=0.01,
            max_drawdown_pct=0.09,
        )
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)
    grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
        run_output_root=grid_root,
    )

    with pytest.raises(CoordinationError, match="already exists and is non-empty"):
        grid_module.run_staged_grid(
            resolved,
            model_group="banknifty_futures/h15_tp_auto",
            profile_id="openfe_v9_dual",
            run_output_root=grid_root,
        )


def test_staged_grid_resume_reuses_completed_lane_summaries(tmp_path: Path, monkeypatch) -> None:
    parquet_root = build_staged_parquet_root(tmp_path)
    base_manifest_path = build_staged_smoke_manifest(tmp_path, parquet_root)
    grid_manifest_path = build_staged_grid_manifest(tmp_path, base_manifest_path)
    resolved = load_and_resolve_manifest(grid_manifest_path, validate_paths=True)
    grid_root = tmp_path / "grid_root_resume"
    calls: list[str] = []

    def _fake_run_research(resolved_config, *, run_output_root=None, run_reuse_mode="fail_if_exists"):
        output_root = Path(run_output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        run_name = str(resolved_config["outputs"]["run_name"])
        calls.append(run_name)
        summary = _mock_summary(
            run_name=run_name,
            publishable=False,
            stage2_auc=0.52,
            stage2_brier=0.25,
            profit_factor=1.05,
            net_return_sum=0.01,
            max_drawdown_pct=0.09,
        )
        summary["output_root"] = str(output_root)
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    monkeypatch.setattr(grid_module, "run_research", _fake_run_research)
    first = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
        run_output_root=grid_root,
    )
    calls_after_first = list(calls)
    resumed = grid_module.run_staged_grid(
        resolved,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
        run_output_root=grid_root,
        run_reuse_mode="resume",
    )

    assert resumed["grid_run_id"] == first["grid_run_id"]
    assert calls == calls_after_first
