from __future__ import annotations

from pathlib import Path

from ml_pipeline_2.publishing import release_recovery_run
from ml_pipeline_2.tests.helpers import build_recovery_smoke_manifest, build_synthetic_feature_frames


class _CompletedProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = "synced"
        self.stderr = ""


def test_recovery_release_runs_end_to_end_and_syncs_to_gcs(tmp_path: Path, monkeypatch) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    monkeypatch.setenv("MODEL_SWITCH_REPO_ROOT", str(tmp_path))

    calls: list[list[str]] = []

    def _fake_run(cmd, capture_output, text, check):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _CompletedProcess()

    monkeypatch.setattr("ml_pipeline_2.publishing.release.subprocess.run", _fake_run)

    payload = release_recovery_run(
        config=manifest_path,
        model_group="banknifty_futures/h15_tp_auto",
        profile_id="openfe_v9_dual",
        model_bucket_url="gs://unit-test-models/published_models",
    )

    assert payload["status"] == "completed"
    assert payload["assessment"]["publishable"] is True
    assert payload["publish"]["release_assessment"]["publishable"] is True
    assert payload["threshold_sweep"]["status"] == "completed"
    assert payload["gcs_sync"]["status"] == "completed"
    assert calls
    assert calls[0][0:3] == ["gcloud", "storage", "rsync"]
    assert calls[0][-1] == "--recursive"
    assert payload["gcs_sync"]["target_url"] == "gs://unit-test-models/published_models/banknifty_futures/h15_tp_auto"

    assessment_path = Path(str(payload["paths"]["assessment"]))
    runtime_env_path = Path(str(payload["paths"]["runtime_env"]))
    release_summary_path = Path(str(payload["paths"]["release_summary"]))
    assert assessment_path.exists()
    assert runtime_env_path.exists()
    assert release_summary_path.exists()

    runtime_env = runtime_env_path.read_text(encoding="utf-8")
    assert "STRATEGY_ENGINE=ml_pure" in runtime_env
    assert f"ML_PURE_RUN_ID={payload['run_id']}" in runtime_env
    assert "ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto" in runtime_env
