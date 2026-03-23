from __future__ import annotations

from pathlib import Path

from ml_pipeline_2.experiment_control import background


def test_launch_background_job_writes_job_metadata(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class _FakeProcess:
        pid = 4242

    def _fake_popen(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(background, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(background.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(background, "_is_process_running", lambda pid: True)

    payload = background.launch_background_job(
        module="ml_pipeline_2.run_research",
        args=["--config", "test.json"],
        job_name="Research Job",
        metadata={"summary_filename": "summary.json"},
        job_root=tmp_path / "jobs",
    )

    assert payload["status"] == "running"
    assert payload["pid"] == 4242
    job_path = Path(payload["job_dir"]) / "job.json"
    assert job_path.exists()
    assert calls["kwargs"]["cwd"] == str(tmp_path)

