from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..contracts.manifests import PHASE2_LABEL_SWEEP_KIND, RECOVERY_KIND, STAGED_KIND, load_and_resolve_manifest
from .state import RunContext


def _timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _scenario_runner(kind: str):
    if kind == PHASE2_LABEL_SWEEP_KIND:
        from ..scenario_flows.phase2_label_sweep import run_phase2_label_sweep

        return run_phase2_label_sweep
    if kind == RECOVERY_KIND:
        from ..scenario_flows.fo_expiry_aware_recovery import run_recovery_research

        return run_recovery_research
    if kind == STAGED_KIND:
        from ..scenario_flows.staged_dual_recipe import run_staged_dual_recipe

        return run_staged_dual_recipe
    raise ValueError(f"unsupported experiment kind: {kind}")


def run_research(resolved_config: Dict[str, Any], *, run_output_root: Optional[Path] = None) -> Dict[str, Any]:
    out_root = (
        Path(run_output_root).resolve()
        if run_output_root is not None
        else Path(resolved_config["outputs"]["artifacts_root"]) / f"{resolved_config['outputs']['run_name']}_{_timestamp_suffix()}"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    resolved = dict(resolved_config)
    resolved["outputs"] = dict(resolved_config["outputs"])
    resolved["outputs"]["run_output_root"] = str(out_root.resolve())
    ctx = RunContext(output_root=out_root, resolved_config=resolved)
    ctx.write_json("resolved_config.json", json.loads(json.dumps(resolved, default=str)))
    ctx.write_text("manifest_hash.txt", str(resolved.get("manifest_hash", "")))
    ctx.append_state("job_start", experiment_kind=str(resolved["experiment_kind"]), output_root=str(out_root.resolve()))
    runner = _scenario_runner(str(resolved["experiment_kind"]))
    summary = runner(ctx)
    if isinstance(summary, dict):
        summary["output_root"] = str(out_root.resolve())
    ctx.append_state("job_done", status=str(summary.get("status", "completed")))
    return summary


def run_manifest(manifest_path: Path, *, validate_only: bool = False, run_output_root: Optional[Path] = None) -> Dict[str, Any]:
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    if validate_only:
        return {"status": "validated", "resolved_config": resolved}
    return run_research(resolved, run_output_root=run_output_root)
