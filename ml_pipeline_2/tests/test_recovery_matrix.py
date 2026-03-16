from __future__ import annotations

import json
from pathlib import Path

from ml_pipeline_2.run_recovery_matrix import (
    _build_parser,
    _resolve_args,
    generate_recovery_matrix,
    launch_pending_recovery_matrix_jobs,
    refresh_recovery_matrix_report,
    watch_pending_recovery_matrix_jobs,
)
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


TUNED_TREE_MODELS = [
    "xgb_shallow",
    "xgb_balanced",
    "xgb_regularized",
    "xgb_deep_v1",
    "xgb_deep_slow_v1",
    "lgbm_fast",
    "lgbm_dart",
    "lgbm_large_v1",
    "lgbm_large_dart_v1",
]


def test_generate_recovery_matrix_writes_combo_manifests(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=None,
        models=["logreg_balanced"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=False,
        job_root=None,
    )

    assert index["recipe_count"] == 2
    assert len(index["combos"]) == 1
    manifest_path = Path(index["combos"][0]["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["catalog"]["feature_sets"] == ["fo_expiry_aware_v2"]
    assert payload["scenario"]["primary_model"] == "logreg_balanced"


def test_generate_recovery_matrix_writes_combo_manifests_for_tuned_models(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[15],
        tp_grid=[0.0030],
        sl_grid=[0.0012],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=None,
        models=["xgb_deep_v1", "lgbm_large_v1"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=False,
        job_root=None,
    )

    combo_models = {combo["primary_model"] for combo in index["combos"]}
    assert combo_models == {"xgb_deep_v1", "lgbm_large_v1"}
    for combo in index["combos"]:
        payload = json.loads(Path(combo["manifest_path"]).read_text(encoding="utf-8"))
        assert payload["catalog"]["models"] == [combo["primary_model"]]
        assert payload["scenario"]["primary_model"] == combo["primary_model"]


def test_refresh_recovery_matrix_report_summarizes_completed_combo(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"
    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=None,
        models=["logreg_balanced"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=False,
        job_root=None,
    )
    combo = index["combos"][0]
    recipe = index["recipes"][0]
    output_root = Path(combo["artifacts_root"]) / "run_20240101_000000"
    output_root.mkdir(parents=True, exist_ok=True)
    summary_payload = {
        "status": "completed",
        "selected_primary_recipe_id": recipe["recipe_id"],
        "primary_recipes": [
            {
                "recipe": dict(recipe),
                "holdout_summary": {
                    "stage_a_passed": True,
                    "side_share_in_band": True,
                    "profit_factor": 1.7,
                    "net_return_sum": 0.12,
                    "long_share": 0.52,
                    "trades": 42,
                },
            }
        ],
        "meta_gate": {
            "holdout_summary": {
                "stage_a_passed": True,
                "side_share_in_band": True,
                "profit_factor": 1.9,
                "net_return_sum": 0.15,
                "ce_share": 0.51,
                "trades": 30,
            }
        },
    }
    (output_root / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    report = refresh_recovery_matrix_report(matrix_root)

    assert report["completed_count"] == 1
    assert report["recommended_combo_key"] == combo["combo_key"]
    assert (matrix_root / "report.csv").exists()
    assert (matrix_root / "recipe_report.csv").exists()


def test_generate_recovery_matrix_respects_max_parallel_launches(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"
    launches = []

    def _fake_launch_background_job(*, module, args, job_name, metadata, job_root):
        job_dir = tmp_path / "jobs" / f"{job_name}_job"
        job_dir.mkdir(parents=True, exist_ok=True)
        launches.append(job_name)
        return {"job_id": f"{job_name}_id", "job_dir": str(job_dir)}

    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.launch_background_job", _fake_launch_background_job)
    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.refresh_recovery_matrix_report", lambda matrix_root: {"matrix_root": str(matrix_root)})

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed"],
        recipes_override=None,
        models=["logreg_balanced", "xgb_shallow"],
        feature_sets=["fo_expiry_aware_v2", "fo_no_time_context"],
        recipe_fanout=False,
        launch_background=True,
        job_root=tmp_path / "jobs",
        max_parallel_launches=1,
    )

    assert len(launches) == 1
    assert len(index["combos"]) == 4
    assert "background_job_path" in index["combos"][0]
    assert "background_job_path" not in index["combos"][1]
    assert index["max_parallel_launches"] == 1


def test_launch_pending_recovery_matrix_jobs_fills_to_parallel_cap(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"
    launches = []

    def _fake_launch_background_job(*, module, args, job_name, metadata, job_root):
        job_dir = tmp_path / "jobs" / f"{job_name}_job_{len(launches)}"
        job_dir.mkdir(parents=True, exist_ok=True)
        launches.append(job_name)
        return {"job_id": f"{job_name}_id_{len(launches)}", "job_dir": str(job_dir)}

    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.launch_background_job", _fake_launch_background_job)
    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.refresh_recovery_matrix_report", lambda matrix_root: {"combos": [{"status": "running"}, {"status": "running"}]})

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed"],
        recipes_override=None,
        models=["logreg_balanced", "xgb_shallow"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=True,
        job_root=tmp_path / "jobs",
        max_parallel_launches=1,
    )
    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.get_background_job_status", lambda **kwargs: {"status": "running"})

    payload = launch_pending_recovery_matrix_jobs(matrix_root, max_parallel=2, job_root=tmp_path / "jobs")
    updated = json.loads((matrix_root / "matrix.json").read_text(encoding="utf-8"))

    assert len(launches) == 2
    assert len(payload["launched_combo_keys"]) == 1
    assert "background_job_path" in updated["combos"][1]
    assert updated["max_parallel_launches"] == 2


def test_tuning_matrix_configs_resolve_expected_search_space() -> None:
    parser = _build_parser()

    args_1m = parser.parse_args(["--config", "ml_pipeline_2/configs/research/recovery_matrix.tuning_1m_e2e.json"])
    resolved_1m = _resolve_args(args_1m)
    assert resolved_1m["models"] == TUNED_TREE_MODELS
    assert resolved_1m["feature_sets"] == ["fo_expiry_aware_v2"]
    assert resolved_1m["tp_grid"] == [0.003]
    assert resolved_1m["sl_grid"] == [0.0012]
    assert resolved_1m["horizon_grid"] == [15]
    assert resolved_1m["barrier_modes"] == ["fixed"]
    assert resolved_1m["max_parallel"] == 3

    args_5m = parser.parse_args(["--config", "ml_pipeline_2/configs/research/recovery_matrix.tuning_5m.json"])
    resolved_5m = _resolve_args(args_5m)
    assert resolved_5m["models"] == TUNED_TREE_MODELS
    assert resolved_5m["feature_sets"] == ["fo_expiry_aware_v2", "fo_oi_pcr_momentum", "fo_no_time_context"]
    assert resolved_5m["tp_grid"] == [0.002, 0.0025, 0.003]
    assert resolved_5m["sl_grid"] == [0.0008, 0.001, 0.0012]
    assert resolved_5m["horizon_grid"] == [15, 20]
    assert resolved_5m["barrier_modes"] == ["fixed", "atr_scaled"]
    assert resolved_5m["max_parallel"] == 3

    args_4y = parser.parse_args(["--config", "ml_pipeline_2/configs/research/recovery_matrix.tuning_4y.json"])
    resolved_4y = _resolve_args(args_4y)
    assert resolved_4y["models"] == TUNED_TREE_MODELS
    assert resolved_4y["feature_sets"] == ["fo_expiry_aware_v2", "fo_oi_pcr_momentum", "fo_no_time_context"]
    assert resolved_4y["tp_grid"] == [0.002, 0.0025, 0.003]
    assert resolved_4y["sl_grid"] == [0.0008, 0.001, 0.0012]
    assert resolved_4y["horizon_grid"] == [15, 20]
    assert resolved_4y["barrier_modes"] == ["fixed", "atr_scaled"]
    assert resolved_4y["max_parallel"] == 8
    assert resolved_4y["poll_seconds"] == 120
    assert resolved_4y["recipes"] == []

    shortlist_args = parser.parse_args(["--config", "ml_pipeline_2/configs/research/recovery_matrix.shortlist_4y.json"])
    shortlist_resolved = _resolve_args(shortlist_args)
    assert shortlist_resolved["models"] == ["xgb_balanced", "xgb_regularized", "xgb_shallow"]
    assert shortlist_resolved["feature_sets"] == ["fo_no_time_context"]
    assert shortlist_resolved["max_parallel"] == 1
    assert len(shortlist_resolved["recipes"]) == 4
    assert {recipe["recipe_id"] for recipe in shortlist_resolved["recipes"]} == {
        "FIXED_H15_TP25_SL10",
        "FIXED_H15_TP20_SL8",
        "FIXED_H15_TP25_SL12",
        "FIXED_H15_TP20_SL10",
    }

    fast_path_args = parser.parse_args(["--config", "ml_pipeline_2/configs/research/recovery_matrix.fast_path_4y.json"])
    fast_path_resolved = _resolve_args(fast_path_args)
    assert fast_path_resolved["models"] == ["xgb_shallow", "xgb_regularized"]
    assert fast_path_resolved["feature_sets"] == ["fo_expiry_aware_v2", "fo_oi_pcr_momentum"]
    assert fast_path_resolved["recipe_fanout"] is True
    assert fast_path_resolved["max_parallel"] == 16
    assert fast_path_resolved["poll_seconds"] == 120
    assert len(fast_path_resolved["recipes"]) == 4
    assert {recipe["recipe_id"] for recipe in fast_path_resolved["recipes"]} == {
        "ATR_H15_TP30_SL8",
        "ATR_H15_TP30_SL10",
        "FIXED_H15_TP30_SL8",
        "FIXED_H15_TP30_SL10",
    }

    watch_args = parser.parse_args(
        [
            "--config",
            "ml_pipeline_2/configs/research/recovery_matrix.tuning_4y.json",
            "--watch-pending",
            "--matrix-root",
            "ml_pipeline_2/artifacts/research_matrices/example",
            "--poll-seconds",
            "45",
        ]
    )
    watch_resolved = _resolve_args(watch_args)
    assert watch_resolved["watch_pending"] is True
    assert watch_resolved["poll_seconds"] == 45


def test_watch_pending_recovery_matrix_jobs_runs_until_completion(tmp_path: Path, monkeypatch) -> None:
    reports = iter(
        [
            {
                "combos": [
                    {"status": "running"},
                    {"status": "pending"},
                    {"status": "completed"},
                ]
            },
            {
                "combos": [
                    {"status": "completed"},
                    {"status": "failed"},
                    {"status": "completed"},
                ]
            },
        ]
    )
    launches = iter(
        [
            {"launched_combo_keys": ["combo_a"], "report": next(reports)},
            {"launched_combo_keys": ["combo_b"], "report": next(reports)},
        ]
    )
    sleeps = []

    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.launch_pending_recovery_matrix_jobs", lambda *args, **kwargs: next(launches))
    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.time.sleep", lambda seconds: sleeps.append(seconds))

    payload = watch_pending_recovery_matrix_jobs(
        tmp_path / "matrix",
        max_parallel=8,
        job_root=tmp_path / "jobs",
        retry_failed=True,
        poll_seconds=7,
    )

    assert payload["iterations"] == 2
    assert payload["launched_combo_keys"] == ["combo_a", "combo_b"]
    assert payload["status_counts"] == {"completed": 2, "running": 0, "pending": 0, "failed": 1}
    assert payload["retry_failed"] is True
    assert sleeps == [7]


def test_watch_pending_requires_positive_poll_seconds(tmp_path: Path) -> None:
    try:
        watch_pending_recovery_matrix_jobs(tmp_path / "matrix", max_parallel=1, job_root=tmp_path / "jobs", poll_seconds=0)
    except ValueError as exc:
        assert "poll_seconds" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-positive poll_seconds")


def test_generate_recovery_matrix_supports_explicit_recipe_list(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[99],
        tp_grid=[0.0099],
        sl_grid=[0.0042],
        barrier_modes=["atr_scaled"],
        recipes_override=[
            {"recipe_id": "FIXED_H15_TP25_SL10", "horizon_minutes": 15, "take_profit_pct": 0.0025, "stop_loss_pct": 0.0010, "barrier_mode": "fixed"},
            {"recipe_id": "FIXED_H15_TP20_SL8", "horizon_minutes": 15, "take_profit_pct": 0.0020, "stop_loss_pct": 0.0008, "barrier_mode": "fixed"},
            {"recipe_id": "FIXED_H15_TP25_SL12", "horizon_minutes": 15, "take_profit_pct": 0.0025, "stop_loss_pct": 0.0012, "barrier_mode": "fixed"},
            {"recipe_id": "FIXED_H15_TP20_SL10", "horizon_minutes": 15, "take_profit_pct": 0.0020, "stop_loss_pct": 0.0010, "barrier_mode": "fixed"},
        ],
        models=["xgb_balanced", "xgb_regularized", "xgb_shallow"],
        feature_sets=["fo_no_time_context"],
        recipe_fanout=False,
        launch_background=False,
        job_root=None,
        max_parallel_launches=1,
    )

    assert index["recipe_count"] == 4
    assert len(index["combos"]) == 3
    assert {recipe["recipe_id"] for recipe in index["recipes"]} == {
        "FIXED_H15_TP25_SL10",
        "FIXED_H15_TP20_SL8",
        "FIXED_H15_TP25_SL12",
        "FIXED_H15_TP20_SL10",
    }


def test_launch_pending_retry_failed_reuses_latest_run_dir(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"
    launches = []

    def _fake_launch_background_job(*, module, args, job_name, metadata, job_root):
        job_dir = tmp_path / "jobs" / f"{job_name}_retry"
        job_dir.mkdir(parents=True, exist_ok=True)
        launches.append({"job_name": job_name, "args": list(args), "metadata": dict(metadata or {})})
        return {"job_id": f"{job_name}_retry_id", "job_dir": str(job_dir)}

    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.launch_background_job", _fake_launch_background_job)
    monkeypatch.setattr("ml_pipeline_2.run_recovery_matrix.refresh_recovery_matrix_report", lambda matrix_root: {"combos": [{"status": "running"}]})

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=None,
        models=["logreg_balanced"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=True,
        job_root=tmp_path / "jobs",
        max_parallel_launches=1,
    )
    combo = index["combos"][0]
    run_dir = Path(combo["artifacts_root"]) / "run_20240101_000000"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "ml_pipeline_2.run_recovery_matrix.get_background_job_status",
        lambda **kwargs: {"status": "failed", "output_root": str(run_dir), "summary_path": None},
    )

    payload = launch_pending_recovery_matrix_jobs(
        matrix_root,
        max_parallel=1,
        job_root=tmp_path / "jobs",
        retry_failed=True,
    )

    assert payload["retry_failed"] is True
    assert payload["launched_combo_keys"] == [combo["combo_key"]]
    assert any("--run-output-root" in launch["args"] for launch in launches)
    assert any(str(run_dir) in launch["args"] for launch in launches)


def test_refresh_recovery_matrix_report_includes_recipe_progress(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"
    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=None,
        models=["logreg_balanced"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=False,
        launch_background=True,
        job_root=tmp_path / "jobs",
        max_parallel_launches=1,
    )
    combo = index["combos"][0]
    output_root = Path(combo["artifacts_root"]) / "run_20240101_000000"
    recipe_root = output_root / "primary_recipes" / "TB_BASE_L1"
    recipe_root.mkdir(parents=True, exist_ok=True)
    (recipe_root / "summary.json").write_text(json.dumps({"recipe": {"recipe_id": "TB_BASE_L1"}, "holdout_summary": {}}), encoding="utf-8")
    (output_root / "state.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts_utc": "2026-03-15T00:00:00+00:00", "event": "job_start"}),
                json.dumps({"ts_utc": "2026-03-15T00:01:00+00:00", "event": "primary_recipe_start", "recipe_id": "TB_BASE_L1"}),
                json.dumps({"ts_utc": "2026-03-15T00:02:00+00:00", "event": "primary_recipe_done", "recipe_id": "TB_BASE_L1"}),
                json.dumps({"ts_utc": "2026-03-15T00:03:00+00:00", "event": "primary_recipe_start", "recipe_id": "TB_ATR_L1"}),
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "ml_pipeline_2.run_recovery_matrix.get_background_job_status",
        lambda **kwargs: {"status": "running", "output_root": str(output_root), "summary_path": None},
    )

    report = refresh_recovery_matrix_report(matrix_root)
    combo_row = report["combos"][0]

    assert combo_row["recipes_completed"] == 1
    assert combo_row["recipes_total"] == 2
    assert combo_row["last_state_event"] == "primary_recipe_start"
    assert combo_row["current_recipe_id"] == "TB_ATR_L1"


def test_generate_recovery_matrix_can_fanout_recipes_into_independent_combos(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    base_manifest = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    matrix_root = tmp_path / "matrix"

    index = generate_recovery_matrix(
        base_manifest_path=base_manifest,
        matrix_root=matrix_root,
        horizon_grid=[2],
        tp_grid=[0.0010],
        sl_grid=[0.0005],
        barrier_modes=["fixed", "atr_scaled"],
        recipes_override=[
            {"recipe_id": "TB_BASE_L1", "horizon_minutes": 2, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "barrier_mode": "fixed"},
            {"recipe_id": "TB_ATR_L1", "horizon_minutes": 2, "take_profit_pct": 0.0010, "stop_loss_pct": 0.0005, "barrier_mode": "atr_scaled"},
        ],
        models=["logreg_balanced", "xgb_shallow"],
        feature_sets=["fo_expiry_aware_v2"],
        recipe_fanout=True,
        launch_background=False,
        job_root=None,
    )

    assert index["recipe_fanout"] is True
    assert index["recipe_count"] == 2
    assert len(index["combos"]) == 4
    assert {combo["recipe_id"] for combo in index["combos"]} == {"TB_BASE_L1", "TB_ATR_L1"}
    for combo in index["combos"]:
        payload = json.loads(Path(combo["manifest_path"]).read_text(encoding="utf-8"))
        assert len(payload["scenario"]["recipes"]) == 1
        assert payload["scenario"]["recipe_selection"] == [combo["recipe_id"]]
        assert payload["scenario"]["recipes"][0]["recipe_id"] == combo["recipe_id"]
