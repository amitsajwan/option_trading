from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from .publish import published_models_root, repo_root


def _load_json(path: Path) -> Dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid run report payload (expected object): {path}")
    return payload


def _resolve_repo_path(path_value: str, *, root: Path) -> Path:
    candidate = Path(str(path_value))
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def resolve_ml_pure_artifacts(run_id: str, model_group: str) -> Dict[str, object]:
    run = str(run_id or "").strip()
    group = str(model_group or "").strip().strip("/\\")
    if not run:
        raise ValueError("run_id must be non-empty")
    if not group:
        raise ValueError("model_group must be non-empty")

    root = repo_root()
    run_report_path = (published_models_root(root=root) / Path(group) / "reports" / "training" / f"run_{run}.json").resolve()
    if not run_report_path.exists():
        raise FileNotFoundError(f"run report not found for run_id={run}: {run_report_path}")

    run_report = _load_json(run_report_path)
    published_paths = run_report.get("published_paths")
    if not isinstance(published_paths, dict):
        raise ValueError(f"run report missing published_paths object: {run_report_path}")

    model_package_raw = str(published_paths.get("model_package") or "").strip()
    threshold_report_raw = str(published_paths.get("threshold_report") or "").strip()
    if not model_package_raw:
        raise ValueError(f"run report missing published_paths.model_package: {run_report_path}")
    if not threshold_report_raw:
        raise ValueError(f"run report missing published_paths.threshold_report: {run_report_path}")

    model_package_path = _resolve_repo_path(model_package_raw, root=root)
    threshold_report_path = _resolve_repo_path(threshold_report_raw, root=root)
    return {
        "model_package_path": str(model_package_path),
        "threshold_report_path": str(threshold_report_path),
        "run_report_path": str(run_report_path),
        "run_report_payload": run_report,
    }


def validate_switch_strict(run_report_payload: Dict[str, object]) -> Tuple[bool, str]:
    if not isinstance(run_report_payload, dict):
        return False, "invalid run report payload"

    decision = ""
    publish_decision = run_report_payload.get("publish_decision")
    if isinstance(publish_decision, dict):
        decision = str(publish_decision.get("decision") or "").strip().upper()
    publish_status = str(run_report_payload.get("publish_status") or "").strip().lower()
    if decision != "PUBLISH" and publish_status != "published":
        return False, f"publish_status={publish_status or decision or 'MISSING'}"

    release_assessment = run_report_payload.get("release_assessment")
    if isinstance(release_assessment, dict) and not bool(release_assessment.get("publishable")):
        reasons = list(release_assessment.get("blocking_reasons") or [])
        reason = ",".join(str(item) for item in reasons) if reasons else "release_assessment_failed"
        return False, f"release_assessment={reason}"

    publish_assessment = run_report_payload.get("publish_assessment")
    if isinstance(publish_assessment, dict) and not bool(publish_assessment.get("publishable")):
        reasons = list(publish_assessment.get("blocking_reasons") or [])
        reason = ",".join(str(item) for item in reasons) if reasons else "publish_assessment_failed"
        return False, f"publish_assessment={reason}"

    published_paths = run_report_payload.get("published_paths")
    if not isinstance(published_paths, dict):
        return False, "missing published_paths"
    model_package_raw = str(published_paths.get("model_package") or "").strip()
    threshold_report_raw = str(published_paths.get("threshold_report") or "").strip()
    if not model_package_raw:
        return False, "missing published_paths.model_package"
    if not threshold_report_raw:
        return False, "missing published_paths.threshold_report"

    root = repo_root()
    model_package_path = _resolve_repo_path(model_package_raw, root=root)
    threshold_report_path = _resolve_repo_path(threshold_report_raw, root=root)
    if not model_package_path.exists():
        return False, f"missing artifact model_package={model_package_path}"
    if not threshold_report_path.exists():
        return False, f"missing artifact threshold_report={threshold_report_path}"
    return True, "ok"
