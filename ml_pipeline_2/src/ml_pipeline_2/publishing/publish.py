from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import joblib

from ..experiment_control.state import utc_now


def repo_root(explicit_root: Optional[Path] = None) -> Path:
    if explicit_root is not None:
        return Path(explicit_root).resolve()
    env_root = str(os.getenv("MODEL_SWITCH_REPO_ROOT") or os.getenv("ML_PIPELINE_2_REPO_ROOT") or "").strip()
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "ml_pipeline_2" / "artifacts").exists():
        return cwd
    guessed = Path(__file__).resolve().parents[4]
    return guessed if (guessed / "ml_pipeline_2").exists() else cwd


def published_models_root(*, root: Optional[Path] = None) -> Path:
    return repo_root(root) / "ml_pipeline_2" / "artifacts" / "published_models"


def _resolve_repo_path(path_value: str | Path, *, root: Path) -> Path:
    candidate = Path(str(path_value))
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _to_rel_repo(path: Path, *, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _copy_file(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _copy_joblib_dict(src: Path, dst: Path) -> Dict[str, Any]:
    payload = joblib.load(src)
    if not isinstance(payload, dict):
        raise ValueError(f"model package must be dict: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(payload, dst)
    return payload


def _normalize_required_features(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _load_runtime_model_package(path: Path) -> Dict[str, Any]:
    package = _copy_joblib_dict(path, path)
    feature_columns = _normalize_required_features(package.get("feature_columns"))
    if not feature_columns:
        raise ValueError(f"model package missing feature_columns: {path}")
    models = package.get("models")
    if not isinstance(models, dict) or "ce" not in models or "pe" not in models:
        raise ValueError(f"published recovery package must contain dual-side models.ce/models.pe: {path}")
    return package


def _extract_required_features(model_package: Dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    input_contract = model_package.get("_model_input_contract")
    if isinstance(input_contract, dict):
        required = _normalize_required_features(input_contract.get("required_features"))
        if required:
            return required, dict(input_contract)
    required = _normalize_required_features(model_package.get("feature_columns"))
    return required, {
        "required_features": required,
        "allow_extra_features": True,
        "missing_policy": "error",
    }


def _extract_thresholds(training_report: Dict[str, Any]) -> tuple[float, float]:
    candidates = [
        training_report,
        dict(training_report.get("trading_utility_config") or {}),
        dict(training_report.get("dual_mode_policy") or {}),
    ]
    for payload in candidates:
        ce = payload.get("ce_threshold")
        pe = payload.get("pe_threshold")
        try:
            if ce is not None and pe is not None:
                return float(ce), float(pe)
        except Exception:
            continue
    raise ValueError("training report missing ce_threshold/pe_threshold")


def _read_threshold_sweep_summary(path: Path) -> Dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"threshold sweep summary must be a JSON object: {path}")
    return payload


def _match_threshold_row(rows: object, *, threshold: float) -> Optional[Dict[str, Any]]:
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        try:
            row_threshold = float(row.get("threshold"))
        except Exception:
            continue
        if abs(row_threshold - float(threshold)) <= 1e-9:
            return dict(row)
    return None


def _resolve_publish_threshold_payload(
    *,
    training_report: Dict[str, Any],
    threshold_source: str,
    threshold_sweep_summary_path: Optional[Path],
) -> tuple[float, float, Optional[Path], Optional[Dict[str, Any]]]:
    normalized = str(threshold_source or "training").strip().lower()
    if normalized == "training":
        ce_threshold, pe_threshold = _extract_thresholds(training_report)
        return float(ce_threshold), float(pe_threshold), None, None
    if normalized != "threshold_sweep_recommended":
        raise ValueError(f"unsupported threshold_source: {threshold_source}")
    if threshold_sweep_summary_path is None or not threshold_sweep_summary_path.exists():
        raise FileNotFoundError(f"threshold sweep summary not found: {threshold_sweep_summary_path}")
    sweep_summary = _read_threshold_sweep_summary(threshold_sweep_summary_path)
    recommended_threshold = sweep_summary.get("recommended_threshold")
    if recommended_threshold is None:
        raise ValueError(f"threshold sweep summary missing recommended_threshold: {threshold_sweep_summary_path}")
    threshold_value = float(recommended_threshold)
    selected_row = dict(sweep_summary.get("recommended_row") or {})
    if not selected_row:
        selected_row = _match_threshold_row(sweep_summary.get("rows"), threshold=threshold_value) or {}
    return threshold_value, threshold_value, threshold_sweep_summary_path, selected_row


def _build_threshold_report(
    *,
    run_id: str,
    model_group: str,
    profile_id: str,
    training_report: Dict[str, Any],
    input_contract: Dict[str, Any],
    threshold_source: str,
    threshold_sweep_summary_path: Optional[Path],
    threshold_sweep_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    ce_threshold, pe_threshold, resolved_sweep_path, resolved_sweep_row = _resolve_publish_threshold_payload(
        training_report=training_report,
        threshold_source=threshold_source,
        threshold_sweep_summary_path=threshold_sweep_summary_path,
    )
    trading_utility_config = dict(training_report.get("trading_utility_config") or {})
    if trading_utility_config:
        trading_utility_config["ce_threshold"] = float(ce_threshold)
        trading_utility_config["pe_threshold"] = float(pe_threshold)
    return {
        "schema_version": "1.0",
        "publisher": "ml_pipeline_2",
        "publish_kind": "recovery_primary_dual_v1",
        "created_at_utc": utc_now(),
        "run_id": run_id,
        "model_group": model_group,
        "profile_id": profile_id,
        "ce_threshold": float(ce_threshold),
        "pe_threshold": float(pe_threshold),
        "threshold_source": str(threshold_source),
        "threshold_sweep_summary_path": (str(resolved_sweep_path.resolve()) if resolved_sweep_path is not None else None),
        "threshold_sweep_row": dict(threshold_sweep_row or resolved_sweep_row or {}),
        "label_target": training_report.get("label_target"),
        "feature_profile": training_report.get("feature_profile"),
        "objective": training_report.get("objective"),
        "trading_utility_config": trading_utility_config,
        "input_contract": input_contract,
    }


def _build_model_contract(
    *,
    run_id: str,
    model_group: str,
    profile_id: str,
    required_features: list[str],
    input_contract: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "publisher": "ml_pipeline_2",
        "publish_kind": "recovery_primary_dual_v1",
        "contract_id": str(input_contract.get("contract_id") or "snapshot_ml_flat_v1"),
        "model_group": model_group,
        "profile_id": profile_id,
        "run_id": run_id,
        "source": "_model_input_contract.required_features",
        "required_features": required_features,
        "allow_extra_features": bool(input_contract.get("allow_extra_features", True)),
        "missing_policy": str(input_contract.get("missing_policy") or "error"),
        "input_contract": input_contract,
    }


def _selected_primary_row(summary_payload: Dict[str, Any]) -> Dict[str, Any]:
    selected_id = str(summary_payload.get("selected_primary_recipe_id") or "").strip()
    if not selected_id:
        raise ValueError("recovery summary missing selected_primary_recipe_id")
    for row in list(summary_payload.get("primary_recipes") or []):
        if not isinstance(row, dict):
            continue
        recipe = row.get("recipe")
        recipe_id = ""
        if isinstance(recipe, dict):
            recipe_id = str(recipe.get("recipe_id") or "").strip()
        if recipe_id == selected_id:
            return row
    raise ValueError(f"selected primary recipe not found in summary: {selected_id}")


def publish_recovery_run(
    *,
    run_dir: str | Path,
    model_group: str,
    profile_id: str,
    threshold_source: str = "training",
    root: Optional[Path] = None,
    allow_unsafe_publish: bool = False,
) -> Dict[str, Any]:
    publish_root = repo_root(root)
    source_run_dir = Path(run_dir).resolve()
    summary_path = source_run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"recovery summary.json not found: {summary_path}")
    summary = _load_json(summary_path)
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"recovery run is not completed: {summary_path}")

    selected_row = _selected_primary_row(summary)
    model_package_path = Path(str(selected_row.get("model_package_path") or "")).resolve()
    training_report_path = Path(str(selected_row.get("training_report_path") or "")).resolve()
    if not model_package_path.exists():
        raise FileNotFoundError(f"selected primary model package not found: {model_package_path}")
    if not training_report_path.exists():
        raise FileNotFoundError(f"selected primary training report not found: {training_report_path}")
    threshold_sweep_summary_path = training_report_path.parent / "threshold_sweep" / "summary.json"

    run_id = str(source_run_dir.name).strip()
    group = str(model_group or "").strip().strip("/\\")
    profile = str(profile_id or "").strip()
    if not group:
        raise ValueError("model_group must be non-empty")
    if not profile:
        raise ValueError("profile_id must be non-empty")

    from .release import assess_recovery_release_candidate

    release_assessment = assess_recovery_release_candidate(
        run_dir=source_run_dir,
        threshold_source=str(threshold_source),
    )
    if not bool(release_assessment.get("publishable")) and not allow_unsafe_publish:
        raise ValueError(
            "recovery run is not publishable without override: "
            + ", ".join(str(reason) for reason in list(release_assessment.get("blocking_reasons") or []))
        )

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

    model_package = _copy_joblib_dict(model_package_path, run_model_path)
    _copy_joblib_dict(model_package_path, active_model_path)
    required_features, input_contract = _extract_required_features(model_package)
    if not required_features:
        raise ValueError(f"published model package has no required features: {model_package_path}")
    if not isinstance(model_package.get("models"), dict) or "ce" not in model_package["models"] or "pe" not in model_package["models"]:
        raise ValueError(f"published model package is not dual-side runtime-usable: {model_package_path}")

    training_report = _load_json(training_report_path)
    _copy_file(training_report_path, run_training_path)
    _copy_file(training_report_path, active_training_path)

    threshold_report = _build_threshold_report(
        run_id=run_id,
        model_group=group,
        profile_id=profile,
        training_report=training_report,
        input_contract=input_contract,
        threshold_source=str(threshold_source),
        threshold_sweep_summary_path=threshold_sweep_summary_path if str(threshold_source).strip().lower() == "threshold_sweep_recommended" else None,
        threshold_sweep_row=None,
    )
    model_contract = _build_model_contract(
        run_id=run_id,
        model_group=group,
        profile_id=profile,
        required_features=required_features,
        input_contract=input_contract,
    )
    _write_json(run_threshold_path, threshold_report)
    _write_json(active_threshold_path, threshold_report)
    _write_json(run_contract_path, model_contract)
    _write_json(active_contract_path, model_contract)

    publish_summary = {
        "created_at_utc": utc_now(),
        "publisher": "ml_pipeline_2",
        "publish_kind": "recovery_primary_dual_v1",
        "publish_status": "published",
        "publish_decision": {"decision": "PUBLISH", "allow_unsafe_publish": bool(allow_unsafe_publish)},
        "publish_override": bool(allow_unsafe_publish and not bool(release_assessment.get("publishable"))),
        "run_id": run_id,
        "model_group": group,
        "profile_id": profile,
        "feature_profile": training_report.get("feature_profile"),
        "objective": training_report.get("objective"),
        "label_target": training_report.get("label_target"),
        "selected_primary_recipe_id": summary.get("selected_primary_recipe_id"),
        "selected_primary_recipe": dict(selected_row.get("recipe") or {}),
        "threshold_source": str(threshold_source),
        "threshold_sweep_summary_path": threshold_report.get("threshold_sweep_summary_path"),
        "threshold_sweep_row": dict(threshold_report.get("threshold_sweep_row") or {}),
        "release_assessment": release_assessment,
        "source_paths": {
            "run_dir": _to_rel_repo(source_run_dir, root=publish_root),
            "summary": _to_rel_repo(summary_path, root=publish_root),
            "model_package": _to_rel_repo(model_package_path, root=publish_root),
            "training_report": _to_rel_repo(training_report_path, root=publish_root),
            "threshold_sweep_summary": (
                _to_rel_repo(Path(str(threshold_report["threshold_sweep_summary_path"])), root=publish_root)
                if threshold_report.get("threshold_sweep_summary_path")
                else None
            ),
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
        "input_contract": input_contract,
    }
    _write_json(run_report_path, publish_summary)
    _write_json(latest_report_path, publish_summary)
    return publish_summary
