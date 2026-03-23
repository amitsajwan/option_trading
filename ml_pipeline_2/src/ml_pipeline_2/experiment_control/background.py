from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "ml_pipeline_2" / "src" / "ml_pipeline_2").exists():
            return candidate
    return Path.cwd().resolve()


def background_jobs_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    return (_repo_root() / "ml_pipeline_2" / "artifacts" / "background_jobs").resolve()


def _sanitize_name(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value))
    collapsed = "_".join(part for part in cleaned.split("_") if part)
    return collapsed or "job"


def _job_id(job_name: str) -> str:
    return f"{_sanitize_name(job_name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_process_running(pid: object) -> bool:
    try:
        pid_value = int(pid)
    except Exception:
        return False
    if pid_value <= 0:
        return False
    try:
        os.kill(pid_value, 0)
    except OSError:
        return False
    return True


def _discover_output_root(metadata: Dict[str, Any]) -> Optional[Path]:
    outputs = dict(metadata.get("outputs") or {})
    explicit_output_root = str(metadata.get("output_root") or "").strip()
    if explicit_output_root:
        candidate = Path(explicit_output_root).resolve()
        if candidate.exists():
            return candidate
    explicit_run_dir = str(metadata.get("run_dir") or "").strip()
    if explicit_run_dir:
        candidate = Path(explicit_run_dir).resolve()
        if candidate.exists():
            return candidate
    artifacts_root = str(outputs.get("artifacts_root") or "").strip()
    run_name = str(outputs.get("run_name") or "").strip()
    if not artifacts_root or not run_name:
        return None
    root = Path(artifacts_root).resolve()
    if not root.exists():
        return None
    candidates = sorted(
        [path for path in root.glob(f"{run_name}_*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _summary_path(output_root: Optional[Path], metadata: Dict[str, Any]) -> Optional[Path]:
    if output_root is None:
        return None
    summary_filename = str(metadata.get("summary_filename") or "summary.json").strip() or "summary.json"
    candidate = output_root / summary_filename
    return candidate if candidate.exists() else None


def launch_background_job(
    *,
    module: str,
    args: Sequence[str],
    job_name: str,
    metadata: Optional[Dict[str, Any]] = None,
    job_root: str | Path | None = None,
) -> Dict[str, Any]:
    root = background_jobs_root(job_root)
    job_id = _job_id(job_name)
    job_dir = root / job_id
    log_path = job_dir / "process.log"
    meta_path = job_dir / "job.json"
    cmd = [sys.executable, "-m", str(module), *[str(arg) for arg in args]]
    creationflags = 0
    extra_popen: Dict[str, Any] = {}
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        extra_popen["start_new_session"] = True
    job_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        handle.write(f"[{utc_now()}] launching {' '.join(cmd)}\n".encode("utf-8"))
        process = subprocess.Popen(
            cmd,
            cwd=str(_repo_root()),
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            **extra_popen,
        )
    record = {
        "job_id": job_id,
        "job_name": str(job_name),
        "created_at_utc": utc_now(),
        "module": str(module),
        "args": [str(arg) for arg in args],
        "pid": int(process.pid),
        "status": "launched",
        "command": cmd,
        "job_dir": str(job_dir.resolve()),
        "log_path": str(log_path.resolve()),
        "metadata": dict(metadata or {}),
    }
    _write_json(meta_path, record)
    return get_background_job_status(job_path=meta_path)


def get_background_job_status(*, job_id: str | None = None, job_path: str | Path | None = None, job_root: str | Path | None = None) -> Dict[str, Any]:
    if job_path is not None:
        path = Path(job_path).resolve()
    elif job_id is not None:
        path = background_jobs_root(job_root) / str(job_id) / "job.json"
    else:
        raise ValueError("job_id or job_path is required")
    record = _read_json(path)
    metadata = dict(record.get("metadata") or {})
    output_root = _discover_output_root(metadata)
    summary_path = _summary_path(output_root, metadata)
    running = _is_process_running(record.get("pid"))
    status = "completed" if summary_path is not None else ("running" if running else "failed")
    out = dict(record)
    out["status"] = status
    out["output_root"] = (str(output_root.resolve()) if output_root is not None else None)
    out["summary_path"] = (str(summary_path.resolve()) if summary_path is not None else None)
    return out
