from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from ..catalog.feature_sets import feature_set_names
from ..catalog.models import model_names


STAGED_KIND = "staged_dual_recipe_v1"
MANIFEST_KINDS = (STAGED_KIND,)


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
    from ..staged.registries import view_registry

    inputs = resolved.get("inputs") or {}
    parquet_root = inputs.get("parquet_root")
    if not isinstance(parquet_root, Path) or not parquet_root.exists():
        errors.append("inputs.parquet_root must point to an existing parquet root")
        return

    support_dataset = str(inputs.get("support_dataset") or "").strip()
    if not support_dataset:
        errors.append("inputs.support_dataset must be non-empty")
    elif not (parquet_root / support_dataset).exists():
        errors.append(f"inputs.support_dataset not found under parquet_root: {support_dataset}")

    views = resolved.get("views") or {}
    registry = view_registry()
    for stage_name in ("stage1", "stage2", "stage3"):
        view_id = str(views.get(f"{stage_name}_view_id") or "").strip()
        if not view_id or view_id not in registry:
            continue
        dataset_name = str(registry[view_id].dataset_name)
        if not (parquet_root / dataset_name).exists():
            errors.append(f"views.{stage_name}_view_id dataset not found under parquet_root: {dataset_name}")


def _validate_staged_catalog(payload: Dict[str, Any], errors: List[str]) -> None:
    from ..staged.recipes import recipe_catalog_ids

    models_by_stage = payload.get("models_by_stage")
    if not isinstance(models_by_stage, dict):
        errors.append("catalog.models_by_stage must be an object")
    feature_sets_by_stage = payload.get("feature_sets_by_stage")
    if not isinstance(feature_sets_by_stage, dict):
        errors.append("catalog.feature_sets_by_stage must be an object")

    valid_models = set(model_names())
    valid_feature_sets = set(feature_set_names())
    for stage_name in ("stage1", "stage2", "stage3"):
        stage_models = list((models_by_stage or {}).get(stage_name) or [])
        if not stage_models:
            errors.append(f"catalog.models_by_stage.{stage_name} must not be empty")
        unknown_models = sorted(set(str(item) for item in stage_models) - valid_models)
        if unknown_models:
            errors.append(f"unknown staged models for {stage_name}: {unknown_models}; valid options: {model_names()}")

        stage_feature_sets = list((feature_sets_by_stage or {}).get(stage_name) or [])
        if not stage_feature_sets:
            errors.append(f"catalog.feature_sets_by_stage.{stage_name} must not be empty")
        unknown_feature_sets = sorted(set(str(item) for item in stage_feature_sets) - valid_feature_sets)
        if unknown_feature_sets:
            errors.append(
                f"unknown staged feature sets for {stage_name}: {unknown_feature_sets}; valid options: {feature_set_names()}"
            )

    recipe_catalog_id = str(payload.get("recipe_catalog_id") or "").strip()
    if not recipe_catalog_id:
        errors.append("catalog.recipe_catalog_id must not be empty")
    elif recipe_catalog_id not in set(recipe_catalog_ids()):
        errors.append(f"catalog.recipe_catalog_id must be one of {recipe_catalog_ids()}")


def _validate_staged_components(payload: Dict[str, Any], errors: List[str]) -> None:
    from ..staged.registries import label_registry, policy_registry, publish_registry, trainer_registry, view_registry

    views = payload.get("views") or {}
    labels = payload.get("labels") or {}
    training = payload.get("training") or {}
    policy = payload.get("policy") or {}
    publish = payload.get("publish") or {}

    valid_views = set(view_registry())
    valid_labels = set(label_registry())
    valid_trainers = set(trainer_registry())
    valid_policies = set(policy_registry())
    valid_publishers = set(publish_registry())

    for stage_name in ("stage1", "stage2", "stage3"):
        view_id = str(views.get(f"{stage_name}_view_id") or "").strip()
        if view_id not in valid_views:
            errors.append(f"views.{stage_name}_view_id must be one of {sorted(valid_views)}")

        labeler_id = str(labels.get(f"{stage_name}_labeler_id") or "").strip()
        if labeler_id not in valid_labels:
            errors.append(f"labels.{stage_name}_labeler_id must be one of {sorted(valid_labels)}")

        trainer_id = str(training.get(f"{stage_name}_trainer_id") or "").strip()
        if trainer_id not in valid_trainers:
            errors.append(f"training.{stage_name}_trainer_id must be one of {sorted(valid_trainers)}")

        policy_id = str(policy.get(f"{stage_name}_policy_id") or "").strip()
        if policy_id not in valid_policies:
            errors.append(f"policy.{stage_name}_policy_id must be one of {sorted(valid_policies)}")

    publisher_id = str(publish.get("publisher_id") or "").strip()
    if publisher_id not in valid_publishers:
        errors.append(f"publish.publisher_id must be one of {sorted(valid_publishers)}")


def _validate_staged_runtime(payload: Dict[str, Any], errors: List[str]) -> None:
    gate_ids = list(payload.get("prefilter_gate_ids") or [])
    if not gate_ids:
        errors.append("runtime.prefilter_gate_ids must not be empty")
    if "block_expiry" in payload and not isinstance(payload.get("block_expiry"), bool):
        errors.append("runtime.block_expiry must be boolean")


def _validate_staged_training(payload: Dict[str, Any], errors: List[str]) -> None:
    preprocess = payload.get("preprocess")
    if not isinstance(preprocess, dict):
        errors.append("training.preprocess must be an object")

    cv_config = payload.get("cv_config")
    if not isinstance(cv_config, dict):
        errors.append("training.cv_config must be an object")
        cv_config = {}
    for key in ("train_days", "valid_days", "test_days", "step_days"):
        try:
            if int(cv_config.get(key, 0)) <= 0:
                raise ValueError
        except Exception:
            errors.append(f"training.cv_config.{key} must be > 0")

    objectives = payload.get("objectives_by_stage")
    if not isinstance(objectives, dict):
        errors.append("training.objectives_by_stage must be an object")
    for stage_name in ("stage1", "stage2", "stage3"):
        objective = str((objectives or {}).get(stage_name) or "").strip().lower()
        if objective not in {"brier", "rmse"}:
            errors.append(f"training.objectives_by_stage.{stage_name} must be one of ['brier', 'rmse']")

    if "random_state" not in payload:
        errors.append("training.random_state must be set")
    else:
        try:
            if int(payload.get("random_state")) < 0:
                raise ValueError
        except Exception:
            errors.append("training.random_state must be an integer >= 0")

    runtime = payload.get("runtime") or {}
    try:
        if int(runtime.get("model_n_jobs", 0)) <= 0:
            raise ValueError
    except Exception:
        errors.append("training.runtime.model_n_jobs must be an integer > 0")

    stage2_label_filter = payload.get("stage2_label_filter") or {}
    if stage2_label_filter and not isinstance(stage2_label_filter, dict):
        errors.append("training.stage2_label_filter must be an object")
        stage2_label_filter = {}
    if stage2_label_filter:
        if "enabled" in stage2_label_filter and not isinstance(stage2_label_filter.get("enabled"), bool):
            errors.append("training.stage2_label_filter.enabled must be boolean")
        if "min_directional_edge_after_cost" in stage2_label_filter:
            try:
                if float(stage2_label_filter.get("min_directional_edge_after_cost")) < 0.0:
                    raise ValueError
            except Exception:
                errors.append(
                    "training.stage2_label_filter.min_directional_edge_after_cost must be a number >= 0"
                )

    search_options_by_stage = payload.get("search_options_by_stage") or {}
    if search_options_by_stage and not isinstance(search_options_by_stage, dict):
        errors.append("training.search_options_by_stage must be an object")
        search_options_by_stage = {}
    for stage_name in ("stage1", "stage2", "stage3"):
        stage_options = search_options_by_stage.get(stage_name) or {}
        if stage_options and not isinstance(stage_options, dict):
            errors.append(f"training.search_options_by_stage.{stage_name} must be an object")
            continue
        hpo = stage_options.get("hpo") or {}
        if hpo and not isinstance(hpo, dict):
            errors.append(f"training.search_options_by_stage.{stage_name}.hpo must be an object")
            continue
        if not hpo:
            continue
        strategy = str(hpo.get("strategy", "random")).strip().lower()
        if strategy not in {"random"}:
            errors.append(f"training.search_options_by_stage.{stage_name}.hpo.strategy must be 'random'")
        try:
            if int(hpo.get("trials_per_model", 0)) <= 0:
                raise ValueError
        except Exception:
            errors.append(f"training.search_options_by_stage.{stage_name}.hpo.trials_per_model must be an integer > 0")
        if "sampler_seed" in hpo:
            try:
                int(hpo.get("sampler_seed"))
            except Exception:
                errors.append(f"training.search_options_by_stage.{stage_name}.hpo.sampler_seed must be an integer")

    try:
        if float(payload.get("cost_per_trade")) < 0.0:
            raise ValueError
    except Exception:
        errors.append("training.cost_per_trade must be numeric and >= 0")


def _validate_staged_policy(payload: Dict[str, Any], errors: List[str]) -> None:
    stage1 = payload.get("stage1") or {}
    stage2 = payload.get("stage2") or {}
    stage3 = payload.get("stage3") or {}
    if not list(stage1.get("threshold_grid") or []):
        errors.append("policy.stage1.threshold_grid must not be empty")
    if not list(stage2.get("ce_threshold_grid") or []):
        errors.append("policy.stage2.ce_threshold_grid must not be empty")
    if not list(stage2.get("pe_threshold_grid") or []):
        errors.append("policy.stage2.pe_threshold_grid must not be empty")
    if not list(stage2.get("min_edge_grid") or []):
        errors.append("policy.stage2.min_edge_grid must not be empty")
    if not list(stage3.get("threshold_grid") or []):
        errors.append("policy.stage3.threshold_grid must not be empty")
    if not list(stage3.get("margin_grid") or []):
        errors.append("policy.stage3.margin_grid must not be empty")


def _validate_staged_hard_gates(payload: Dict[str, Any], errors: List[str]) -> None:
    sections = {
        "stage1": ("roc_auc_min", "brier_max", "roc_auc_drift_half_split_max_abs"),
        "stage2": ("roc_auc_min", "brier_max"),
        "stage3": ("max_drawdown_slack",),
        "combined": (
            "profit_factor_min",
            "max_drawdown_pct_max",
            "trades_min",
            "net_return_sum_min",
            "side_share_min",
            "side_share_max",
            "block_rate_min",
        ),
    }
    for section, required_keys in sections.items():
        block = payload.get(section)
        if not isinstance(block, dict):
            errors.append(f"hard_gates.{section} must be an object")
            continue
        for key in required_keys:
            if key not in block:
                errors.append(f"hard_gates.{section}.{key} must be set")
                continue
            try:
                value = float(block[key])
            except Exception:
                errors.append(f"hard_gates.{section}.{key} must be numeric")
                continue
            if section in {"stage1", "stage2"} and key in {"roc_auc_min", "brier_max", "roc_auc_drift_half_split_max_abs"}:
                if value < 0.0:
                    errors.append(f"hard_gates.{section}.{key} must be >= 0")
            elif section == "stage3":
                if value < 0.0:
                    errors.append("hard_gates.stage3.max_drawdown_slack must be >= 0")
            elif section == "combined":
                if key == "trades_min" and int(value) < 0:
                    errors.append("hard_gates.combined.trades_min must be >= 0")
                elif key in {"side_share_min", "side_share_max", "block_rate_min", "max_drawdown_pct_max"}:
                    if value < 0.0 or value > 1.0:
                        errors.append(f"hard_gates.combined.{key} must be in [0,1]")
                elif key == "profit_factor_min" and value < 1.0:
                    errors.append("hard_gates.combined.profit_factor_min must be >= 1.0")
        if section == "combined" and isinstance(block, dict):
            try:
                side_share_min = float(block["side_share_min"])
                side_share_max = float(block["side_share_max"])
                if side_share_min > side_share_max:
                    errors.append("hard_gates.combined.side_share_min must be <= side_share_max")
            except Exception:
                pass


def _validate_windows(windows_payload: Dict[str, Any], errors: List[str]) -> Dict[str, Dict[str, str]]:
    resolved: Dict[str, Dict[str, str]] = {}
    for key in ("research_train", "research_valid", "full_model", "final_holdout"):
        if key not in windows_payload:
            errors.append(f"missing windows.{key}")
            continue
        resolved[key] = _validate_window(key, dict(windows_payload.get(key) or {}), errors)

    if all(key in resolved for key in ("research_train", "research_valid", "full_model", "final_holdout")):
        if resolved["research_train"]["end"] >= resolved["research_valid"]["start"]:
            errors.append("research_train must end before research_valid starts")
        if resolved["full_model"]["start"] > resolved["research_train"]["start"]:
            errors.append("full_model.start must be on or before research_train.start")
        if resolved["full_model"]["end"] < resolved["research_valid"]["end"]:
            errors.append("full_model.end must be on or after research_valid.end")
        if resolved["full_model"]["end"] >= resolved["final_holdout"]["start"]:
            errors.append("full_model window must end before final_holdout starts")
    return resolved


def manifest_hash(payload: Dict[str, Any]) -> str:
    return sha256(_json_dump(payload).encode("utf-8")).hexdigest()


def resolve_manifest(payload: Dict[str, Any], *, manifest_path: Path, validate_paths: bool = True) -> Dict[str, Any]:
    errors: List[str] = []
    required_sections = (
        "schema_version",
        "experiment_kind",
        "inputs",
        "outputs",
        "catalog",
        "windows",
        "views",
        "labels",
        "training",
        "policy",
        "runtime",
        "publish",
        "hard_gates",
    )
    _require_sections(payload, required_sections, errors)

    kind = str(payload.get("experiment_kind") or "").strip()
    if kind not in MANIFEST_KINDS:
        errors.append(f"experiment_kind must be one of {sorted(MANIFEST_KINDS)}")

    manifest_dir = manifest_path.resolve().parent
    inputs_payload = dict(payload.get("inputs") or {})
    outputs_payload = dict(payload.get("outputs") or {})
    catalog_payload = dict(payload.get("catalog") or {})
    training_payload = dict(payload.get("training") or {})

    resolved = {
        "schema_version": int(payload.get("schema_version", 0)),
        "experiment_kind": kind,
        "manifest_path": str(manifest_path.resolve()),
        "inputs": {
            "parquet_root": _normalize_path(inputs_payload.get("parquet_root"), manifest_dir=manifest_dir),
            "support_dataset": str(inputs_payload.get("support_dataset") or "").strip(),
        },
        "outputs": {
            "artifacts_root": _normalize_path(outputs_payload.get("artifacts_root"), manifest_dir=manifest_dir),
            "run_name": str(outputs_payload.get("run_name") or "").strip(),
        },
        "catalog": catalog_payload,
        "windows": _validate_windows(dict(payload.get("windows") or {}), errors),
        "views": dict(payload.get("views") or {}),
        "labels": dict(payload.get("labels") or {}),
        "training": training_payload,
        "policy": dict(payload.get("policy") or {}),
        "runtime": dict(payload.get("runtime") or {}),
        "publish": dict(payload.get("publish") or {}),
        "hard_gates": dict(payload.get("hard_gates") or {}),
    }

    if not resolved["outputs"]["artifacts_root"]:
        errors.append("outputs.artifacts_root must be set for staged manifests")
    if not resolved["outputs"]["run_name"]:
        errors.append("outputs.run_name must be set for staged manifests")

    _validate_staged_catalog(catalog_payload, errors)
    _validate_staged_components(payload, errors)
    _validate_staged_training(training_payload, errors)
    _validate_staged_runtime(dict(payload.get("runtime") or {}), errors)
    _validate_staged_policy(dict(payload.get("policy") or {}), errors)
    _validate_staged_hard_gates(dict(payload.get("hard_gates") or {}), errors)

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
