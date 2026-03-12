from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .experiment_control.background import get_background_job_status, launch_background_job


TARGET_TO_MODULE = {
    "research": "ml_pipeline_2.run_research",
    "move_detector_quick": "ml_pipeline_2.run_move_detector_quick",
    "direction_from_move_quick": "ml_pipeline_2.run_direction_from_move_quick",
}


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(value: object, *, config_dir: Path) -> str:
    if value is None:
        return ""
    txt = str(value).strip()
    if not txt:
        return ""
    path = Path(txt)
    return str(path.resolve() if path.is_absolute() else (config_dir / path).resolve())


def _launch_metadata(target: str, config_path: Path) -> Dict[str, Any]:
    payload = _read_json(config_path)
    config_dir = config_path.resolve().parent
    if target == "research":
        outputs = dict(payload.get("outputs") or {})
        experiment_kind = str(payload.get("experiment_kind") or "").strip()
        summary_filename = "phase2_summary.json" if experiment_kind == "phase2_label_sweep_v1" else "summary.json"
        return {
            "config_path": str(config_path.resolve()),
            "summary_filename": summary_filename,
            "experiment_kind": experiment_kind,
            "outputs": {
                "artifacts_root": _resolve_path(outputs.get("artifacts_root") or "ml_pipeline_2/artifacts/research", config_dir=config_dir),
                "run_name": str(outputs.get("run_name") or experiment_kind or "research"),
            },
        }
    outputs = dict(payload.get("outputs") or {})
    run_dir = _resolve_path(outputs.get("run_dir"), config_dir=config_dir)
    return {
        "config_path": str(config_path.resolve()),
        "summary_filename": "summary.json",
        "run_dir": run_dir or None,
        "outputs": {
            "artifacts_root": _resolve_path(outputs.get("out_root") or "ml_pipeline_2/artifacts/research", config_dir=config_dir),
            "run_name": str(outputs.get("run_name") or target),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch or inspect detached ml_pipeline_2 jobs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="Launch a detached background job")
    launch.add_argument("--target", required=True, choices=sorted(TARGET_TO_MODULE), help="Entry point to launch")
    launch.add_argument("--config", required=True, help="Config JSON path")
    launch.add_argument("--job-name", help="Optional human-readable job name")
    launch.add_argument("--job-root", help="Optional background job registry root")
    launch.add_argument("--resume", action="store_true", help="Pass --resume to quick runners")

    status = subparsers.add_parser("status", help="Inspect background job status")
    status.add_argument("--job-id", help="Background job id")
    status.add_argument("--job-path", help="Explicit path to job.json")
    status.add_argument("--job-root", help="Optional background job registry root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "launch":
        config_path = Path(args.config).resolve()
        module = TARGET_TO_MODULE[str(args.target)]
        job_name = str(args.job_name or f"{args.target}_{config_path.stem}")
        module_args = ["--config", str(config_path)]
        if bool(args.resume) and str(args.target) != "research":
            module_args.append("--resume")
        payload = launch_background_job(
            module=module,
            args=module_args,
            job_name=job_name,
            metadata=_launch_metadata(str(args.target), config_path),
            job_root=args.job_root,
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    payload = get_background_job_status(job_id=args.job_id, job_path=args.job_path, job_root=args.job_root)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
