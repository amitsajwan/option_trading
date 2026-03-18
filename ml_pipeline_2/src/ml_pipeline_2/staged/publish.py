from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import joblib

from ..contracts.manifests import load_and_resolve_manifest
from ..experiment_control.runner import run_research
from ..experiment_control.state import utc_now
from ..publishing.publish import published_models_root, repo_root
from ..publishing.release import sync_published_model_group_to_gcs
from .recipes import get_recipe_catalog
from .registries import view_registry
from .runtime_contract import STAGED_RUNTIME_BUNDLE_KIND, STAGED_RUNTIME_POLICY_KIND, validate_recipe_catalog_payload


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_env(path: Path, payload: Dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{key}={value}" for key, value in payload.items()) + "\n", encoding="utf-8")
    return path


def _to_rel_repo(path: Path, *, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def _load_stage_model(path_value: str) -> Dict[str, Any]:
    payload = joblib.load(Path(path_value))
    if not isinstance(payload, dict):
        raise ValueError(f"stage model package must be dict: {path_value}")
    return payload


def assess_staged_release_candidate(*, run_dir: str | Path) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary_path = source_run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"staged summary.json not found: {summary_path}")
    summary = _load_json(summary_path)
    publish_assessment = dict(summary.get("publish_assessment") or {})
    decision = str(publish_assessment.get("decision") or "HOLD").strip().upper()
    return {
        "created_at_utc": utc_now(),
        "run_dir": str(source_run_dir),
        "run_id": str(source_run_dir.name),
        "publishable": decision == "PUBLISH" and bool(publish_assessment.get("publishable", False)),
        "decision": decision,
        "blocking_reasons": list(publish_assessment.get("blocking_reasons") or []),
        "summary_path": str(summary_path),
        "summary": summary,
    }


def _build_runtime_bundle(summary: Dict[str, Any], *, model_group: str, profile_id: str) -> Dict[str, Any]:
    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    component_ids = dict(summary.get("component_ids") or {})
    recipe_catalog = validate_recipe_catalog_payload(
        [recipe.to_dict() for recipe in get_recipe_catalog(str(summary["recipe_catalog_id"]))]
    )
    stage1_package = _load_stage_model(str(stage_artifacts["stage1"]["model_package_path"]))
    stage2_package = _load_stage_model(str(stage_artifacts["stage2"]["model_package_path"]))
    stage3_artifacts = dict(stage_artifacts.get("stage3") or {})
    recipe_artifacts = dict(stage3_artifacts.get("recipe_artifacts") or {})
    stage3_recipe_ids = list(stage3_artifacts["recipes"])
    if not recipe_artifacts:
        raise ValueError("stage3 recipe_artifacts must be present in staged summary")
    views = view_registry()
    stage1_view = views[str(component_ids["stage1"]["view_id"])].dataset_name
    stage2_view = views[str(component_ids["stage2"]["view_id"])].dataset_name
    stage3_view = views[str(component_ids["stage3"]["view_id"])].dataset_name
    stage3_packages = {
        str(recipe_id): _load_stage_model(str(Path(recipe_artifacts[str(recipe_id)]["model_package_path"]).resolve()))
        for recipe_id in stage3_recipe_ids
    }
    return {
        "kind": STAGED_RUNTIME_BUNDLE_KIND,
        "created_at_utc": utc_now(),
        "run_id": str(summary["run_id"]),
        "model_group": str(model_group),
        "profile_id": str(profile_id),
        "recipe_catalog": recipe_catalog,
        "runtime": {
            "prefilter_gate_ids": list(summary.get("runtime_prefilter_gate_ids") or []),
        },
        "stages": {
            "stage1": {
                "model_package": stage1_package,
                "view_name": stage1_view,
            },
            "stage2": {
                "model_package": stage2_package,
                "view_name": stage2_view,
            },
            "stage3": {
                "recipe_packages": stage3_packages,
                "view_name": stage3_view,
            },
        },
    }


def _build_runtime_policy(summary: Dict[str, Any], *, model_group: str, profile_id: str) -> Dict[str, Any]:
    recipe_catalog = validate_recipe_catalog_payload(
        [recipe.to_dict() for recipe in get_recipe_catalog(str(summary["recipe_catalog_id"]))]
    )
    policy_reports = dict(summary.get("policy_reports") or {})
    return {
        "kind": STAGED_RUNTIME_POLICY_KIND,
        "created_at_utc": utc_now(),
        "run_id": str(summary["run_id"]),
        "model_group": str(model_group),
        "profile_id": str(profile_id),
        "stage1": dict(policy_reports.get("stage1") or {}),
        "stage2": dict(policy_reports.get("stage2") or {}),
        "stage3": dict(policy_reports.get("stage3") or {}),
        "runtime": {
            "prefilter_gate_ids": list(summary.get("runtime_prefilter_gate_ids") or []),
        },
        "recipe_catalog": recipe_catalog,
    }


def publish_staged_run(
    *,
    run_dir: str | Path,
    model_group: str,
    profile_id: str,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    publish_root = repo_root(root)
    assessment = assess_staged_release_candidate(run_dir=run_dir)
    if not assessment["publishable"]:
        raise ValueError(
            "staged run is not publishable: " + ", ".join(str(item) for item in list(assessment["blocking_reasons"]))
        )
    source_run_dir = Path(run_dir).resolve()
    summary = dict(assessment["summary"])
    run_id = str(source_run_dir.name)
    group = str(model_group or "").strip().strip("/\\")
    profile = str(profile_id or "").strip()
    if not group:
        raise ValueError("model_group must be non-empty")
    if not profile:
        raise ValueError("profile_id must be non-empty")

    group_root = published_models_root(root=publish_root) / Path(group)
    data_run_root = group_root / "data" / "training_runs" / run_id
    run_model_path = data_run_root / "model" / "model.joblib"
    run_threshold_path = data_run_root / "config" / "profiles" / profile / "threshold_report.json"
    run_training_path = data_run_root / "config" / "profiles" / profile / "training_report.json"
    run_contract_path = data_run_root / "model_contract.json"

    active_model_path = group_root / "model" / "model.joblib"
    active_threshold_path = group_root / "config" / "profiles" / profile / "threshold_report.json"
    active_training_path = group_root / "config" / "profiles" / profile / "training_report.json"
    active_contract_path = group_root / "model_contract.json"
    run_report_path = group_root / "reports" / "training" / f"run_{run_id}.json"
    latest_report_path = group_root / "reports" / "training" / "latest.json"

    runtime_bundle = _build_runtime_bundle(summary, model_group=group, profile_id=profile)
    runtime_policy = _build_runtime_policy(summary, model_group=group, profile_id=profile)
    run_model_path.parent.mkdir(parents=True, exist_ok=True)
    active_model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(runtime_bundle, run_model_path)
    joblib.dump(runtime_bundle, active_model_path)
    _write_json(run_threshold_path, runtime_policy)
    _write_json(active_threshold_path, runtime_policy)
    _write_json(run_training_path, summary)
    _write_json(active_training_path, summary)

    model_contract = {
        "kind": STAGED_RUNTIME_BUNDLE_KIND,
        "run_id": run_id,
        "model_group": group,
        "profile_id": profile,
        "stage_artifacts": dict(summary.get("stage_artifacts") or {}),
        "component_ids": dict(summary.get("component_ids") or {}),
    }
    _write_json(run_contract_path, model_contract)
    _write_json(active_contract_path, model_contract)

    publish_summary = {
        "created_at_utc": utc_now(),
        "publisher": "ml_pipeline_2",
        "publish_kind": STAGED_RUNTIME_BUNDLE_KIND,
        "publish_status": "published",
        "publish_decision": {"decision": "PUBLISH"},
        "run_id": run_id,
        "model_group": group,
        "profile_id": profile,
        "publish_assessment": {
            "publishable": True,
            "decision": "PUBLISH",
            "blocking_reasons": [],
        },
        "published_paths": {
            "model_package": _to_rel_repo(run_model_path, root=publish_root),
            "threshold_report": _to_rel_repo(run_threshold_path, root=publish_root),
            "training_report": _to_rel_repo(run_training_path, root=publish_root),
            "model_contract": _to_rel_repo(run_contract_path, root=publish_root),
            "data_run_dir": _to_rel_repo(data_run_root, root=publish_root),
        },
        "active_group_paths": {
            "model_package": _to_rel_repo(active_model_path, root=publish_root),
            "threshold_report": _to_rel_repo(active_threshold_path, root=publish_root),
            "training_report": _to_rel_repo(active_training_path, root=publish_root),
            "model_contract": _to_rel_repo(active_contract_path, root=publish_root),
        },
        "report_paths": {
            "run_report": _to_rel_repo(run_report_path, root=publish_root),
            "latest_report": _to_rel_repo(latest_report_path, root=publish_root),
        },
    }
    _write_json(run_report_path, publish_summary)
    _write_json(latest_report_path, publish_summary)
    return publish_summary


def release_staged_run(
    *,
    config: Optional[str | Path] = None,
    run_dir: Optional[str | Path] = None,
    model_group: str,
    profile_id: str,
    run_output_root: Optional[Path] = None,
    model_bucket_url: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    if bool(config) == bool(run_dir):
        raise ValueError("exactly one of config or run_dir must be provided")
    research_summary: Optional[Dict[str, Any]] = None
    if config is not None:
        manifest_path = Path(config).resolve()
        resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
        research_summary = run_research(
            resolved,
            run_output_root=(Path(run_output_root).resolve() if run_output_root is not None else None),
        )
        resolved_run_dir = Path(str(research_summary["output_root"])).resolve()
    else:
        resolved_run_dir = Path(str(run_dir)).resolve()

    assessment = assess_staged_release_candidate(run_dir=resolved_run_dir)
    release_root = resolved_run_dir / "release"
    assessment_path = _write_json(release_root / "assessment.json", assessment)
    publish_summary = publish_staged_run(
        run_dir=resolved_run_dir,
        model_group=model_group,
        profile_id=profile_id,
        root=root,
    )
    runtime_env = {
        "STRATEGY_ENGINE": "ml_pure",
        "ML_PURE_RUN_ID": str(publish_summary["run_id"]),
        "ML_PURE_MODEL_GROUP": str(publish_summary["model_group"]),
    }
    runtime_env_path = _write_env(release_root / "ml_pure_runtime.env", runtime_env)
    gcs_sync = None
    if model_bucket_url:
        gcs_sync = sync_published_model_group_to_gcs(
            model_bucket_url=str(model_bucket_url),
            model_group=str(publish_summary["model_group"]),
            root=root,
        )
    result = {
        "created_at_utc": utc_now(),
        "status": "completed",
        "run_dir": str(resolved_run_dir),
        "run_id": str(publish_summary["run_id"]),
        "model_group": str(publish_summary["model_group"]),
        "profile_id": str(publish_summary["profile_id"]),
        "research_summary": research_summary,
        "assessment": assessment,
        "publish": publish_summary,
        "gcs_sync": gcs_sync,
        "live_handoff": {"engine": "ml_pure", "env": runtime_env},
        "paths": {
            "assessment": str(assessment_path.resolve()),
            "runtime_env": str(runtime_env_path.resolve()),
        },
    }
    summary_path = _write_json(release_root / "release_summary.json", result)
    result["paths"]["release_summary"] = str(summary_path.resolve())
    return result
