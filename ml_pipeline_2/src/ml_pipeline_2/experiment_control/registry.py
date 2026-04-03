from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .state import RunContext, utc_now
from .status import RunIntegrity, RunLifecycleStatus, infer_run_lifecycle_status


def _status_payload(
    *,
    entity_kind: str,
    entity_id: str,
    output_root: Path,
    manifest_hash: str,
    lifecycle_status: RunLifecycleStatus,
    integrity: RunIntegrity,
    reuse_mode: str,
    archived_root: Optional[str] = None,
    parent_root: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "entity_kind": str(entity_kind),
        "entity_id": str(entity_id),
        "output_root": str(output_root.resolve()),
        "manifest_hash": str(manifest_hash or ""),
        "status": str(lifecycle_status),
        "integrity": str(integrity),
        "run_reuse_mode": str(reuse_mode),
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
        "archived_root": archived_root,
        "parent_root": parent_root,
    }
    if extra:
        payload.update(extra)
    return payload


def initialize_run_status(
    ctx: RunContext,
    *,
    run_reuse_mode: str,
    archived_root: Optional[str],
) -> Dict[str, Any]:
    integrity: RunIntegrity = "restarted" if archived_root else "clean"
    payload = _status_payload(
        entity_kind="research_run",
        entity_id=str(ctx.output_root.name),
        output_root=ctx.output_root,
        manifest_hash=str(ctx.resolved_config.get("manifest_hash", "")),
        lifecycle_status="running",
        integrity=integrity,
        reuse_mode=run_reuse_mode,
        archived_root=archived_root,
        parent_root=archived_root,
    )
    ctx.write_json("run_status.json", payload)
    return payload


def finalize_run_status(
    ctx: RunContext,
    *,
    summary: Dict[str, Any],
    run_reuse_mode: str,
    archived_root: Optional[str],
) -> Dict[str, Any]:
    current = _status_payload(
        entity_kind="research_run",
        entity_id=str(ctx.output_root.name),
        output_root=ctx.output_root,
        manifest_hash=str(ctx.resolved_config.get("manifest_hash", "")),
        lifecycle_status=infer_run_lifecycle_status(summary),
        integrity="restarted" if archived_root else "clean",
        reuse_mode=run_reuse_mode,
        archived_root=archived_root,
        parent_root=archived_root,
        extra={
            "completion_mode": str(summary.get("completion_mode") or ""),
            "publishable": bool((dict(summary.get("publish_assessment") or {})).get("publishable", False)),
        },
    )
    ctx.write_json("run_status.json", current)
    return current


def initialize_grid_status(
    *,
    grid_root: Path,
    grid_run_id: str,
    manifest_hash: str,
    run_reuse_mode: str,
    archived_root: Optional[str],
    max_parallel_runs: int,
) -> Dict[str, Any]:
    integrity: RunIntegrity = "restarted" if archived_root else "clean"
    payload = _status_payload(
        entity_kind="staged_grid",
        entity_id=grid_run_id,
        output_root=grid_root,
        manifest_hash=manifest_hash,
        lifecycle_status="running",
        integrity=integrity,
        reuse_mode=run_reuse_mode,
        archived_root=archived_root,
        parent_root=archived_root,
        extra={"max_parallel_runs": int(max_parallel_runs)},
    )
    (grid_root / "grid_status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def finalize_grid_status(
    *,
    grid_root: Path,
    grid_run_id: str,
    manifest_hash: str,
    run_reuse_mode: str,
    archived_root: Optional[str],
    lifecycle_status: RunLifecycleStatus,
    dominant_failure_reason: Optional[str],
    winner_run_id: Optional[str],
) -> Dict[str, Any]:
    integrity: RunIntegrity = "restarted" if archived_root else "clean"
    payload = _status_payload(
        entity_kind="staged_grid",
        entity_id=grid_run_id,
        output_root=grid_root,
        manifest_hash=manifest_hash,
        lifecycle_status=lifecycle_status,
        integrity=integrity,
        reuse_mode=run_reuse_mode,
        archived_root=archived_root,
        parent_root=archived_root,
        extra={
            "dominant_failure_reason": dominant_failure_reason,
            "winner_run_id": winner_run_id,
        },
    )
    (grid_root / "grid_status.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


__all__ = [
    "finalize_grid_status",
    "finalize_run_status",
    "initialize_grid_status",
    "initialize_run_status",
]
