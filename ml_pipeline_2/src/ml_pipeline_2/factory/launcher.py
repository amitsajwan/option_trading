from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from .spec import LaneSpec


@dataclass(frozen=True)
class LaunchResult:
    pid: int
    lane_root: Path
    runner_output_root: Path
    log_path: Path
    command: tuple[str, ...]


class LaneLauncher:
    def __init__(self) -> None:
        self._processes: Dict[int, subprocess.Popen[str]] = {}

    def _build_command(self, lane: LaneSpec, *, runner_output_root: Path) -> list[str]:
        args = [sys.executable]
        if lane.lane_kind == "staged_grid":
            args.extend(
                [
                    "-m",
                    "ml_pipeline_2.run_staged_grid",
                    "--config",
                    str(lane.config_path),
                    "--run-output-root",
                    str(runner_output_root),
                    "--run-reuse-mode",
                    "resume",
                    "--model-group",
                    str(lane.model_group),
                    "--profile-id",
                    str(lane.profile_id),
                ]
            )
            if lane.model_bucket_url:
                args.extend(["--model-bucket-url", str(lane.model_bucket_url)])
            return args

        module_name = "ml_pipeline_2.run_staged_release" if lane.runner_mode == "release" else "ml_pipeline_2.run_research"
        args.extend(
            [
                "-m",
                module_name,
                "--config",
                str(lane.config_path),
                "--run-output-root",
                str(runner_output_root),
                "--run-reuse-mode",
                "resume",
            ]
        )
        if lane.runner_mode == "release":
            args.extend(["--model-group", str(lane.model_group), "--profile-id", str(lane.profile_id)])
            if lane.model_bucket_url:
                args.extend(["--model-bucket-url", str(lane.model_bucket_url)])
        return args

    def launch(self, lane: LaneSpec, *, lane_root: Path) -> LaunchResult:
        lane_root = Path(lane_root).resolve()
        lane_root.mkdir(parents=True, exist_ok=True)
        runner_output_root = lane_root / "runner_output"
        log_path = lane_root / "factory_lane.log"
        args = self._build_command(lane, runner_output_root=runner_output_root)
        with log_path.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(  # noqa: S603
                args,
                cwd=str(Path.cwd()),
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self._processes[int(process.pid)] = process
        return LaunchResult(
            pid=int(process.pid),
            lane_root=lane_root,
            runner_output_root=runner_output_root,
            log_path=log_path,
            command=tuple(str(item) for item in args),
        )

    def is_alive(self, pid: Optional[int]) -> bool:
        if pid is None:
            return False
        process = self._processes.get(int(pid))
        if process is not None:
            return process.poll() is None
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True

    def exit_code(self, pid: Optional[int]) -> Optional[int]:
        if pid is None:
            return None
        process = self._processes.get(int(pid))
        if process is None:
            return None
        return process.poll()

    def terminate(self, pid: Optional[int]) -> None:
        if pid is None:
            return
        process = self._processes.get(int(pid))
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


__all__ = ["LaneLauncher", "LaunchResult"]
