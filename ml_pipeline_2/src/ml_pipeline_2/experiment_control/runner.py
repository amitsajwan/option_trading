from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..contracts.manifests import STAGED_KIND, load_and_resolve_manifest
from .coordination import RunReuseMode, acquire_directory_lock, prepare_output_root
from .registry import finalize_run_status, initialize_run_status
from .state import RunContext, utc_now


def _timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _scenario_runner(kind: str):
    if kind == STAGED_KIND:
        from ..staged import run_staged_research

        return run_staged_research
    raise ValueError(f"unsupported experiment kind: {kind}")


def _scenario_environment_validator(kind: str):
    if kind == STAGED_KIND:
        from ..staged.pipeline import validate_staged_research_environment

        return validate_staged_research_environment
    raise ValueError(f"unsupported experiment kind: {kind}")


class ResearchRunFailed(RuntimeError):
    def __init__(self, message: str, *, output_root: Path) -> None:
        super().__init__(message)
        self.output_root = Path(output_root).resolve()


def _failure_summary(resolved_config: Dict[str, Any], *, out_root: Path, error: Exception) -> Dict[str, Any]:
    return {
        "summary_schema_version": 3,
        "created_at_utc": utc_now(),
        "status": "failed",
        "experiment_kind": str(resolved_config.get("experiment_kind") or ""),
        "run_id": str(out_root.name),
        "completion_mode": "failed",
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }


def validate_runtime_environment(resolved_config: Dict[str, Any]) -> Dict[str, Any]:
    validator = _scenario_environment_validator(str(resolved_config["experiment_kind"]))
    return validator(resolved_config)


def run_research(
    resolved_config: Dict[str, Any],
    *,
    run_output_root: Optional[Path] = None,
    run_reuse_mode: RunReuseMode = "fail_if_exists",
) -> Dict[str, Any]:
    validate_runtime_environment(resolved_config)
    requested_out_root = (
        Path(run_output_root).resolve()
        if run_output_root is not None
        else Path(resolved_config["outputs"]["artifacts_root"]) / f"{resolved_config['outputs']['run_name']}_{_timestamp_suffix()}"
    )
    prep = prepare_output_root(
        requested_out_root,
        reuse_mode=run_reuse_mode,
        summary_filename="summary.json",
        entity_name="research run root",
        lock_filename=".run.lock",
    )
    existing_summary = prep.get("existing_summary")
    if isinstance(existing_summary, dict):
        return existing_summary
    out_root = Path(prep["output_root"]).resolve()
    archived_root = str(prep.get("archived_root") or "") or None
    resolved = dict(resolved_config)
    resolved["outputs"] = dict(resolved_config["outputs"])
    resolved["outputs"]["run_output_root"] = str(out_root.resolve())
    ctx = RunContext(output_root=out_root, resolved_config=resolved)
    with acquire_directory_lock(
        out_root,
        lock_filename=".run.lock",
        entity_name="research run",
        manifest_hash=str(resolved.get("manifest_hash", "")),
    ):
        initialize_run_status(
            ctx,
            run_reuse_mode=str(run_reuse_mode),
            archived_root=archived_root,
        )
        ctx.write_json("resolved_config.json", json.loads(json.dumps(resolved, default=str)))
        ctx.write_text("manifest_hash.txt", str(resolved.get("manifest_hash", "")))
        ctx.append_state(
            "job_start",
            experiment_kind=str(resolved["experiment_kind"]),
            output_root=str(out_root.resolve()),
            run_reuse_mode=str(run_reuse_mode),
        )
        runner = _scenario_runner(str(resolved["experiment_kind"]))
        try:
            summary = runner(ctx)
        except Exception as exc:
            summary = _failure_summary(resolved, out_root=out_root, error=exc)
            summary["output_root"] = str(out_root.resolve())
            summary["execution_integrity"] = "clean"
            summary["execution_policy"] = {
                "run_reuse_mode": str(run_reuse_mode),
                "archived_root": archived_root,
                "run_status_path": str(ctx.run_status_path.resolve()),
            }
            ctx.write_json("summary.json", summary)
            finalize_run_status(
                ctx,
                summary=summary,
                run_reuse_mode=str(run_reuse_mode),
                archived_root=archived_root,
            )
            ctx.append_state(
                "job_failed",
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise ResearchRunFailed(str(exc), output_root=out_root) from exc
        if isinstance(summary, dict):
            summary["output_root"] = str(out_root.resolve())
            summary["execution_integrity"] = "clean"
            summary["execution_policy"] = {
                "run_reuse_mode": str(run_reuse_mode),
                "archived_root": archived_root,
                "run_status_path": str(ctx.run_status_path.resolve()),
            }
            ctx.write_json("summary.json", summary)
            finalize_run_status(
                ctx,
                summary=summary,
                run_reuse_mode=str(run_reuse_mode),
                archived_root=archived_root,
            )
        ctx.append_state("job_done", status=str(summary.get("status", "completed")))
        return summary


def run_manifest(
    manifest_path: Path,
    *,
    validate_only: bool = False,
    run_output_root: Optional[Path] = None,
    run_reuse_mode: RunReuseMode = "fail_if_exists",
) -> Dict[str, Any]:
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    if validate_only:
        return {"status": "validated", "resolved_config": resolved, "runtime_environment": validate_runtime_environment(resolved)}
    return run_research(resolved, run_output_root=run_output_root, run_reuse_mode=run_reuse_mode)
