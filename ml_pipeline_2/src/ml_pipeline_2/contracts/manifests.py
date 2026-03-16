from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from ..catalog.feature_sets import feature_set_names
from ..catalog.models import model_names
from .types import FEATURE_PROFILES, LABEL_TARGET_CHOICES


PHASE2_LABEL_SWEEP_KIND = "phase2_label_sweep_v1"
RECOVERY_KIND = "fo_expiry_aware_recovery_v1"
MANIFEST_KINDS = (PHASE2_LABEL_SWEEP_KIND, RECOVERY_KIND)
CANDIDATE_FILTER_FIELDS = {
    "require_event_sampled",
    "exclude_expiry_day",
    "exclude_regime_atr_high",
    "require_tradeable_context",
    "allow_near_expiry_context",
}


class ManifestValidationError(ValueError):
    pass


def _json_dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, indent=2)


def _normalize_path(value: Any, *, manifest_dir: Path) -> Optional[Path]:
    if value is None:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    candidate = Path(txt)
    if not candidate.is_absolute():
        candidate = (manifest_dir / candidate).resolve()
    return candidate


def _as_date(value: Any, *, field: str, errors: List[str]) -> Optional[str]:
    try:
        dt = pd.Timestamp(str(value))
    except Exception:
        errors.append(f"{field} must be a valid ISO date")
        return None
    if pd.isna(dt):
        errors.append(f"{field} must be a valid ISO date")
        return None
    return dt.strftime("%Y-%m-%d")


def _validate_window(name: str, payload: Dict[str, Any], errors: List[str]) -> Dict[str, str]:
    start = _as_date(payload.get("start"), field=f"windows.{name}.start", errors=errors)
    end = _as_date(payload.get("end"), field=f"windows.{name}.end", errors=errors)
    if start and end and start > end:
        errors.append(f"windows.{name} has start after end")
    return {"start": start or "", "end": end or ""}


def _require_sections(payload: Dict[str, Any], required: Iterable[str], errors: List[str]) -> None:
    for section in required:
        if section not in payload:
            errors.append(f"missing required section: {section}")


def _validate_paths(resolved: Dict[str, Any], errors: List[str]) -> None:
    inputs = resolved.get("inputs") or {}
    for key in ("model_window_features_path", "holdout_features_path", "base_path"):
        path = inputs.get(key)
        if not isinstance(path, Path) or not path.exists():
            errors.append(f"inputs.{key} must point to an existing path")
    baseline_path = inputs.get("baseline_json_path")
    if baseline_path is not None and isinstance(baseline_path, Path) and not baseline_path.exists():
        errors.append("inputs.baseline_json_path must point to an existing path when provided")


def _validate_catalog(payload: Dict[str, Any], errors: List[str]) -> None:
    feature_profile = str(payload.get("feature_profile", "")).strip().lower()
    if feature_profile not in FEATURE_PROFILES:
        errors.append(f"catalog.feature_profile must be one of {sorted(FEATURE_PROFILES)}")
    feature_sets = list(payload.get("feature_sets") or [])
    if not feature_sets:
        errors.append("catalog.feature_sets must not be empty")
    unknown_feature_sets = sorted(set(str(x) for x in feature_sets) - set(feature_set_names()))
    if unknown_feature_sets:
        errors.append(f"unknown feature sets: {unknown_feature_sets}; valid options: {feature_set_names()}")
    models = list(payload.get("models") or [])
    if not models:
        errors.append("catalog.models must not be empty")
    unknown_models = sorted(set(str(x) for x in models) - set(model_names()))
    if unknown_models:
        errors.append(f"unknown models: {unknown_models}; valid options: {model_names()}")


def _validate_model_reference(value: Any, *, field: str, catalog_models: Iterable[str], errors: List[str]) -> None:
    model_name = str(value or "").strip()
    valid_models = set(model_names())
    allowed_models = {str(item) for item in catalog_models}
    if not model_name:
        errors.append(f"{field} must not be empty")
        return
    if model_name not in valid_models:
        errors.append(f"{field} references unknown model: {model_name}; valid options: {model_names()}")
        return
    if model_name not in allowed_models:
        errors.append(f"{field} must be included in catalog.models: {model_name}")


def _validate_model_reference_list(values: Iterable[Any], *, field: str, catalog_models: Iterable[str], errors: List[str]) -> None:
    for idx, value in enumerate(list(values)):
        _validate_model_reference(value, field=f"{field}[{idx}]", catalog_models=catalog_models, errors=errors)


def _validate_training(payload: Dict[str, Any], errors: List[str]) -> None:
    label_target = str(payload.get("label_target", "")).strip().lower()
    if label_target not in LABEL_TARGET_CHOICES:
        errors.append(f"training.label_target must be one of {sorted(LABEL_TARGET_CHOICES)}")
    cv_config = payload.get("cv_config") or {}
    for key in ("train_days", "valid_days", "test_days", "step_days"):
        try:
            if int(cv_config.get(key, 0)) <= 0:
                raise ValueError
        except Exception:
            errors.append(f"training.cv_config.{key} must be > 0")
    utility = payload.get("utility") or {}
    for key in ("ce_threshold", "pe_threshold"):
        try:
            value = float(utility.get(key))
        except Exception:
            errors.append(f"training.utility.{key} must be numeric")
            continue
        if value < 0.0 or value > 1.0:
            errors.append(f"training.utility.{key} must be in [0,1]")
    runtime = payload.get("runtime") or {}
    if "model_n_jobs" in runtime:
        try:
            model_n_jobs = int(runtime.get("model_n_jobs"))
            if model_n_jobs <= 0:
                raise ValueError
        except Exception:
            errors.append("training.runtime.model_n_jobs must be an integer > 0")


def _validate_phase2_scenario(payload: Dict[str, Any], *, catalog_models: Iterable[str], errors: List[str]) -> None:
    recipes = list(payload.get("recipes") or [])
    if not recipes:
        errors.append("scenario.recipes must not be empty")
    seen: set[str] = set()
    for recipe in recipes:
        recipe_id = str((recipe or {}).get("recipe_id", "")).strip()
        if not recipe_id:
            errors.append("scenario.recipes entries require recipe_id")
            continue
        if recipe_id in seen:
            errors.append(f"duplicate scenario recipe_id: {recipe_id}")
        seen.add(recipe_id)
    thresholds = list(payload.get("threshold_grid") or [])
    if not thresholds:
        errors.append("scenario.threshold_grid must not be empty")
    for idx, value in enumerate(thresholds):
        try:
            out = float(value)
        except Exception:
            errors.append(f"scenario.threshold_grid[{idx}] must be numeric")
            continue
        if out < 0.0 or out > 1.0:
            errors.append(f"scenario.threshold_grid[{idx}] must be in [0,1]")
    unknown = sorted(set(str(x) for x in payload.get("baseline_recipe_ids") or []) - seen)
    if unknown:
        errors.append(f"scenario.baseline_recipe_ids reference unknown recipes: {unknown}")
    _validate_model_reference(payload.get("default_model"), field="scenario.default_model", catalog_models=catalog_models, errors=errors)
    stress_models = list(payload.get("stress_models") or [])
    if not stress_models:
        errors.append("scenario.stress_models must not be empty")
    _validate_model_reference_list(stress_models, field="scenario.stress_models", catalog_models=catalog_models, errors=errors)


def _validate_recovery_scenario(payload: Dict[str, Any], *, catalog_models: Iterable[str], errors: List[str]) -> None:
    if not list(payload.get("recipes") or []):
        errors.append("scenario.recipes must not be empty")
    _validate_model_reference(payload.get("primary_model"), field="scenario.primary_model", catalog_models=catalog_models, errors=errors)
    try:
        threshold = float(payload.get("primary_threshold"))
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError
    except Exception:
        errors.append("scenario.primary_threshold must be in [0,1]")
    meta_gate = payload.get("meta_gate") or {}
    grid = list(meta_gate.get("validation_threshold_grid") or [])
    if bool(meta_gate.get("enabled", False)) and not grid:
        errors.append("scenario.meta_gate.validation_threshold_grid must not be empty when meta gate is enabled")
    for idx, value in enumerate(grid):
        try:
            out = float(value)
        except Exception:
            errors.append(f"scenario.meta_gate.validation_threshold_grid[{idx}] must be numeric")
            continue
        if out < 0.0 or out > 1.0:
            errors.append(f"scenario.meta_gate.validation_threshold_grid[{idx}] must be in [0,1]")
    candidate_filter = payload.get("candidate_filter")
    if candidate_filter is not None:
        if not isinstance(candidate_filter, dict):
            errors.append("scenario.candidate_filter must be an object when provided")
        else:
            unknown_keys = sorted(set(str(key) for key in candidate_filter) - CANDIDATE_FILTER_FIELDS)
            if unknown_keys:
                errors.append(f"scenario.candidate_filter contains unknown fields: {unknown_keys}")
            for field in sorted(CANDIDATE_FILTER_FIELDS):
                if field in candidate_filter and not isinstance(candidate_filter.get(field), bool):
                    errors.append(f"scenario.candidate_filter.{field} must be boolean")


def _validate_windows(kind: str, windows_payload: Dict[str, Any], errors: List[str]) -> Dict[str, Dict[str, str]]:
    resolved: Dict[str, Dict[str, str]] = {}
    required = ("research_train", "research_valid", "full_model", "final_holdout") if kind == PHASE2_LABEL_SWEEP_KIND else ("full_model", "final_holdout")
    for key in required:
        if key not in windows_payload:
            errors.append(f"missing windows.{key}")
            continue
        resolved[key] = _validate_window(key, dict(windows_payload.get(key) or {}), errors)
    if kind == PHASE2_LABEL_SWEEP_KIND and all(key in resolved for key in ("research_train", "research_valid", "full_model", "final_holdout")):
        if resolved["research_train"]["end"] >= resolved["research_valid"]["start"]:
            errors.append("research_train must end before research_valid starts")
        if resolved["full_model"]["start"] > resolved["research_train"]["start"] or resolved["full_model"]["end"] < resolved["research_valid"]["end"]:
            errors.append("full_model window must fully contain research_train and research_valid windows")
        if resolved["full_model"]["end"] >= resolved["final_holdout"]["start"]:
            errors.append("full_model window must end before final_holdout starts")
    if kind == RECOVERY_KIND and all(key in resolved for key in ("full_model", "final_holdout")):
        if resolved["full_model"]["end"] >= resolved["final_holdout"]["start"]:
            errors.append("full_model window must end before final_holdout starts")
    return resolved


def manifest_hash(payload: Dict[str, Any]) -> str:
    return sha256(_json_dump(payload).encode("utf-8")).hexdigest()


def resolve_manifest(payload: Dict[str, Any], *, manifest_path: Path, validate_paths: bool = True) -> Dict[str, Any]:
    errors: List[str] = []
    _require_sections(payload, ("schema_version", "experiment_kind", "inputs", "outputs", "catalog", "windows", "training", "scenario"), errors)
    kind = str(payload.get("experiment_kind", "")).strip()
    if kind not in MANIFEST_KINDS:
        errors.append(f"experiment_kind must be one of {sorted(MANIFEST_KINDS)}")
    manifest_dir = manifest_path.resolve().parent
    inputs_payload = dict(payload.get("inputs") or {})
    outputs_payload = dict(payload.get("outputs") or {})
    catalog_payload = dict(payload.get("catalog") or {})
    training_payload = dict(payload.get("training") or {})
    scenario_payload = dict(payload.get("scenario") or {})
    windows_payload = dict(payload.get("windows") or {})
    resolved = {
        "schema_version": int(payload.get("schema_version", 0)),
        "experiment_kind": kind,
        "manifest_path": str(manifest_path.resolve()),
        "inputs": {
            "model_window_features_path": _normalize_path(inputs_payload.get("model_window_features_path"), manifest_dir=manifest_dir),
            "holdout_features_path": _normalize_path(inputs_payload.get("holdout_features_path"), manifest_dir=manifest_dir),
            "base_path": _normalize_path(inputs_payload.get("base_path"), manifest_dir=manifest_dir),
            "baseline_json_path": _normalize_path(inputs_payload.get("baseline_json_path"), manifest_dir=manifest_dir),
        },
        "outputs": {
            "artifacts_root": _normalize_path(outputs_payload.get("artifacts_root") or "ml_pipeline_2/artifacts/research", manifest_dir=manifest_dir),
            "run_name": str(outputs_payload.get("run_name") or kind),
        },
        "catalog": catalog_payload,
        "windows": _validate_windows(kind, windows_payload, errors),
        "training": training_payload,
        "scenario": scenario_payload,
    }
    _validate_catalog(catalog_payload, errors)
    _validate_training(training_payload, errors)
    if kind == PHASE2_LABEL_SWEEP_KIND:
        _validate_phase2_scenario(scenario_payload, catalog_models=list(catalog_payload.get("models") or []), errors=errors)
    elif kind == RECOVERY_KIND:
        _validate_recovery_scenario(scenario_payload, catalog_models=list(catalog_payload.get("models") or []), errors=errors)
    if validate_paths:
        _validate_paths(resolved, errors)
    if errors:
        raise ManifestValidationError("\n".join(errors))
    return resolved


def load_and_resolve_manifest(manifest_path: Path, *, validate_paths: bool = True) -> Dict[str, Any]:
    path = Path(manifest_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    resolved = resolve_manifest(payload, manifest_path=path, validate_paths=validate_paths)
    resolved["manifest_hash"] = manifest_hash(payload)
    resolved["raw_manifest"] = payload
    return resolved
