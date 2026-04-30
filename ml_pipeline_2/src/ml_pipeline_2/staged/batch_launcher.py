"""Batch launcher for running multiple scenario variations on the remote VM.

Usage:
    from ml_pipeline_2.staged.batch_launcher import BatchLauncher

    launcher = BatchLauncher()
    scenarios = scenario_matrix(
        bypass_stage2_values=(False, True),
        stage1_threshold_values=((0.45, 0.5, 0.55, 0.6), (0.5, 0.55),),
    )
    launcher.queue_batch(scenarios)
    launcher.launch_all()
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Sequence

from .scenario_runner import build_manifest, scenario_matrix, write_manifest


DEFAULT_VM_CONFIG = {
    "host": "34.47.131.234",
    "user": "savitasajwan03",
    "ssh_key": r"C:\Users\amits\.ssh\google_compute_engine",
    "known_hosts": r"C:\Users\amits\.ssh\known_hosts",
    "remote_venv_python": "/home/savitasajwan03/option_trading/.venv/bin/python",
    "remote_repo_root": "/home/savitasajwan03/option_trading",
    "remote_config_dir": "/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research",
    "remote_log_dir": "/home/savitasajwan03/option_trading/logs",
    "remote_artifact_root": "/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research",
}


class BatchLauncher:
    """Manages launching multiple training runs on the remote VM via tmux sessions."""

    def __init__(self, vm_config: Dict[str, str] | None = None):
        self.cfg = {**DEFAULT_VM_CONFIG, **(vm_config or {})}
        self.queued: list[Dict[str, Any]] = []
        self.launched: list[Dict[str, Any]] = []

    def _ssh_cmd(self, remote_cmd: str) -> list[str]:
        return [
            "ssh",
            "-i", self.cfg["ssh_key"],
            "-o", f"UserKnownHostsFile={self.cfg['known_hosts']}",
            "-o", "StrictHostKeyChecking=yes",
            f"{self.cfg['user']}@{self.cfg['host']}",
            remote_cmd,
        ]

    def _run_ssh(self, remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(self._ssh_cmd(remote_cmd), capture_output=True, text=True, timeout=timeout)

    def queue(self, manifest: Dict[str, Any], *, run_name: str | None = None) -> None:
        """Queue a single manifest for launch."""
        name = run_name or str(manifest.get("outputs", {}).get("run_name", "unnamed"))
        self.queued.append({"manifest": manifest, "run_name": name})

    def queue_batch(self, manifests: Sequence[Dict[str, Any]]) -> None:
        """Queue multiple manifests."""
        for manifest in manifests:
            name = str(manifest.get("outputs", {}).get("run_name", "unnamed"))
            self.queued.append({"manifest": manifest, "run_name": name})

    def _upload_manifest(self, manifest: Dict[str, Any], run_name: str) -> str:
        """Upload a manifest to the VM and return the remote path."""
        local_path = Path(tempfile.gettempdir()) / f"{run_name}.json"
        write_manifest(manifest, local_path)

        remote_path = f"{self.cfg['remote_config_dir']}/{run_name}.json"
        scp_cmd = [
            "scp",
            "-i", self.cfg["ssh_key"],
            "-o", f"UserKnownHostsFile={self.cfg['known_hosts']}",
            "-o", "StrictHostKeyChecking=yes",
            str(local_path),
            f"{self.cfg['user']}@{self.cfg['host']}:{remote_path}",
        ]
        r = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"SCP failed: {r.stderr}")
        return remote_path

    def _launch_tmux_session(self, run_name: str, config_path: str) -> str:
        """Create a tmux session on the VM and launch the training."""
        session_name = f"batch_{run_name}"
        log_file = f"{self.cfg['remote_log_dir']}/{run_name}.log"
        python_path = self.cfg["remote_repo_root"]
        python_bin = self.cfg["remote_venv_python"]

        # Kill existing session if any
        self._run_ssh(f"tmux kill-session -t {session_name} 2>/dev/null || true", timeout=15)

        # Create tmux session with training command
        cmd = (
            f"cd {python_path} && "
            f"export PYTHONPATH={python_path} && "
            f"{python_bin} -u -m ml_pipeline_2.run_research "
            f"--config {config_path} "
            f"2>&1 | tee {log_file}"
        )
        create_cmd = f"tmux new-session -d -s {session_name} '{cmd}'"
        r = self._run_ssh(create_cmd, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"tmux create failed: {r.stderr}")

        return session_name

    def launch_all(self, *, max_concurrent: int = 2) -> list[Dict[str, Any]]:
        """Launch all queued manifests on the VM. Respects max_concurrent."""
        launched = []
        for item in self.queued:
            run_name = item["run_name"]
            manifest = item["manifest"]

            # Wait if too many concurrent sessions
            while True:
                active = self._count_active_sessions()
                if active < max_concurrent:
                    break
                import time
                time.sleep(30)

            try:
                config_path = self._upload_manifest(manifest, run_name)
                session_name = self._launch_tmux_session(run_name, config_path)
                launched.append({
                    "run_name": run_name,
                    "session_name": session_name,
                    "config_path": config_path,
                    "status": "launched",
                })
            except Exception as e:
                launched.append({
                    "run_name": run_name,
                    "status": "failed",
                    "error": str(e),
                })

        self.launched.extend(launched)
        self.queued.clear()
        return launched

    def _count_active_sessions(self) -> int:
        """Count active tmux sessions for batch runs."""
        r = self._run_ssh("tmux ls 2>/dev/null | grep '^batch_' || true", timeout=15)
        return len([line for line in r.stdout.strip().splitlines() if line.strip()])

    def poll_status(self) -> list[Dict[str, Any]]:
        """Poll the status of all launched runs."""
        results = []
        for item in self.launched:
            session_name = item.get("session_name")
            if not session_name:
                results.append(item)
                continue

            r = self._run_ssh(f"tmux ls 2>/dev/null | grep '^{session_name}:' || echo 'ENDED'", timeout=15)
            session_alive = r.stdout.strip() != "ENDED"

            # Check for summary.json
            artifact_dir = f"{self.cfg['remote_artifact_root']}/{item['run_name']}_*"
            r2 = self._run_ssh(
                f"ls -t {artifact_dir}/summary.json 2>/dev/null | head -n 1 || echo 'NO_SUMMARY'",
                timeout=15,
            )
            has_summary = r2.stdout.strip() != "NO_SUMMARY"

            status = "running" if session_alive else ("completed" if has_summary else "failed")
            results.append({**item, "status": status, "has_summary": has_summary})
        return results


__all__ = ["BatchLauncher", "scenario_matrix"]
