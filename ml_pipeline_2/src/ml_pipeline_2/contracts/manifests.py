from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from ..catalog.feature_sets import feature_set_names
from ..catalog.models import model_names


STAGED_KIND = "staged_dual_recipe_v1"
STAGED_GRID_KIND = "staged_training_grid_v1"
MANIFEST_KINDS = (STAGED_KIND, STAGED_GRID_KIND)
_STAGE_NAMES = ("stage1", "stage2", "stage3")
_STAGE2_SESSION_BUCKETS = ("OPENING", "MORNING", "MIDDAY", "LATE_SESSION")


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
    if "smoke_allow_non_publishable" in publish and not isinstance(publish.get("smoke_allow_non_publishable"), bool):
        errors.append("publish.smoke_allow_non_publishable must be boolean")


def _validate_staged_runtime(payload: Dict[str, Any], errors: List[str]) -> None:
    gate_ids = list(payload.get("prefilter_gate_ids") or [])
    if not gate_ids:
        errors.append("runtime.prefilter_gate_ids must not be empty")
    if "block_expiry" in payload and not isinstance(payload.get("block_expiry"), bool):
        errors.append("runtime.block_expiry must be boolean")


def _validate_stage2_label_filter(payload: Any, errors: List[str], *, field_prefix: str) -> None:
    stage2_label_filter = payload or {}
    if stage2_label_filter and not isinstance(stage2_label_filter, dict):
        errors.append(f"{field_prefix} must be an object")
        return
    if not stage2_label_filter:
        return
    if "enabled" in stage2_label_filter and not isinstance(stage2_label_filter.get("enabled"), bool):
        errors.append(f"{field_prefix}.enabled must be boolean")
    if "min_directional_edge_after_cost" in stage2_label_filter:
        try:
            if float(stage2_label_filter.get("min_directional_edge_after_cost")) < 0.0:
                raise ValueError
        except Exception:
            errors.append(f"{field_prefix}.min_directional_edge_after_cost must be a number >= 0")
    if "require_positive_winner_after_cost" in stage2_label_filter and not isinstance(
        stage2_label_filter.get("require_positive_winner_after_cost"), bool
    ):
        errors.append(f"{field_prefix}.require_positive_winner_after_cost must be boolean")
    if "max_opposing_return_after_cost" in stage2_label_filter:
        try:
            float(stage2_label_filter.get("max_opposing_return_after_cost"))
        except Exception:
            errors.append(f"{field_prefix}.max_opposing_return_after_cost must be numeric")


def _validate_stage2_session_filter(payload: Any, errors: List[str], *, field_prefix: str) -> None:
    stage2_session_filter = payload or {}
    if stage2_session_filter and not isinstance(stage2_session_filter, dict):
        errors.append(f"{field_prefix} must be an object")
        return
    if not stage2_session_filter:
        return
    if "enabled" in stage2_session_filter and not isinstance(stage2_session_filter.get("enabled"), bool):
        errors.append(f"{field_prefix}.enabled must be boolean")
    include_buckets = stage2_session_filter.get("include_buckets")
    if include_buckets is None:
        return
    if not isinstance(include_buckets, list):
        errors.append(f"{field_prefix}.include_buckets must be a list")
        return
    if not include_buckets:
        errors.append(f"{field_prefix}.include_buckets must not be empty")
        return
    normalized = [str(item).strip().upper() for item in include_buckets]
    if len(normalized) != len(set(normalized)):
        errors.append(f"{field_prefix}.include_buckets must not contain duplicates")
    unknown = sorted(set(normalized) - set(_STAGE2_SESSION_BUCKETS))
    if unknown:
        errors.append(
            f"{field_prefix}.include_buckets has unknown buckets: {unknown}; "
            f"valid options: {list(_STAGE2_SESSION_BUCKETS)}"
        )


def _validate_grid_robustness_probe(payload: Any, errors: List[str], *, field_prefix: str) -> None:
    robustness_probe = payload or {}
    if robustness_probe and not isinstance(robustness_probe, dict):
        errors.append(f"{field_prefix} must be an object")
        return
    if not robustness_probe:
        return
    if "enabled" in robustness_probe and not isinstance(robustness_probe.get("enabled"), bool):
        errors.append(f"{field_prefix}.enabled must be boolean")
    for key in ("top_k", "iterations", "random_seed"):
        if key in robustness_probe:
            try:
                if int(robustness_probe.get(key)) <= 0:
                    raise ValueError
            except Exception:
                errors.append(f"{field_prefix}.{key} must be an integer > 0")
    if "resample_unit" in robustness_probe:
        if str(robustness_probe.get("resample_unit") or "").strip().lower() != "trade_date":
            errors.append(f"{field_prefix}.resample_unit must be 'trade_date'")
    if "splits" in robustness_probe:
        splits = robustness_probe.get("splits")
        if not isinstance(splits, list):
            errors.append(f"{field_prefix}.splits must be a list")
        else:
            normalized = [str(item).strip() for item in splits]
            valid = {"research_valid", "final_holdout", "research_train"}
            if not normalized:
                errors.append(f"{field_prefix}.splits must not be empty")
            elif len(normalized) != len(set(normalized)):
                errors.append(f"{field_prefix}.splits must not contain duplicates")
            else:
                unknown = sorted(set(normalized) - valid)
                if unknown:
                    errors.append(
                        f"{field_prefix}.splits has unknown values: {unknown}; "
                        f"valid options: {sorted(valid)}"
                    )


def _validate_search_options_by_stage(payload: Any, errors: List[str], *, field_prefix: str) -> None:
    search_options_by_stage = payload or {}
    if search_options_by_stage and not isinstance(search_options_by_stage, dict):
        errors.append(f"{field_prefix} must be an object")
        return
    if not search_options_by_stage:
        return
    for stage_name in _STAGE_NAMES:
        stage_options = search_options_by_stage.get(stage_name) or {}
        if stage_options and not isinstance(stage_options, dict):
            errors.append(f"{field_prefix}.{stage_name} must be an object")
            continue
        if "max_experiments" in stage_options:
            try:
                if int(stage_options.get("max_experiments", 0)) <= 0:
                    raise ValueError
            except Exception:
                errors.append(f"{field_prefix}.{stage_name}.max_experiments must be an integer > 0")
        if "max_elapsed_seconds" in stage_options:
            try:
                if float(stage_options.get("max_elapsed_seconds", 0.0)) <= 0.0:
                    raise ValueError
            except Exception:
                errors.append(f"{field_prefix}.{stage_name}.max_elapsed_seconds must be numeric and > 0")
        hpo = stage_options.get("hpo") or {}
        if hpo and not isinstance(hpo, dict):
            errors.append(f"{field_prefix}.{stage_name}.hpo must be an object")
            continue
        if not hpo:
            continue
        strategy = str(hpo.get("strategy", "random")).strip().lower()
        if strategy not in {"random"}:
            errors.append(f"{field_prefix}.{stage_name}.hpo.strategy must be 'random'")
        try:
            if int(hpo.get("trials_per_model", 0)) <= 0:
                raise ValueError
        except Exception:
            errors.append(f"{field_prefix}.{stage_name}.hpo.trials_per_model must be an integer > 0")
        if "sampler_seed" in hpo:
            try:
                int(hpo.get("sampler_seed"))
            except Exception:
                errors.append(f"{field_prefix}.{stage_name}.hpo.sampler_seed must be an integer")


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

    _validate_stage2_label_filter(payload.get("stage2_label_filter"), errors, field_prefix="training.stage2_label_filter")
    _validate_stage2_session_filter(
        payload.get("stage2_session_filter"),
        errors,
        field_prefix="training.stage2_session_filter",
    )
    _validate_search_options_by_stage(
        payload.get("search_options_by_stage"),
        errors,
        field_prefix="training.search_options_by_stage",
    )

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


def _validate_grid_run_overrides(payload: Any, errors: List[str], *, field_prefix: str) -> None:
    overrides = payload or {}
    if overrides and not isinstance(overrides, dict):
        errors.append(f"{field_prefix} must be an object")
        return
    if not overrides:
        return

    allowed_top_level = {"catalog", "training", "runtime", "outputs"}
    unknown_top_level = sorted(set(str(key) for key in overrides.keys()) - allowed_top_level)
    if unknown_top_level:
        errors.append(
            f"{field_prefix} contains unsupported top-level overrides: {unknown_top_level}; "
            "allowed keys: ['catalog', 'outputs', 'runtime', 'training']"
        )

    catalog = overrides.get("catalog") or {}
    if catalog:
        if not isinstance(catalog, dict):
            errors.append(f"{field_prefix}.catalog must be an object")
        else:
            unknown_catalog = sorted(set(str(key) for key in catalog.keys()) - {"feature_sets_by_stage"})
            if unknown_catalog:
                errors.append(
                    f"{field_prefix}.catalog supports only feature_sets_by_stage; got unsupported keys: {unknown_catalog}"
                )
            feature_sets_by_stage = catalog.get("feature_sets_by_stage") or {}
            if feature_sets_by_stage and not isinstance(feature_sets_by_stage, dict):
                errors.append(f"{field_prefix}.catalog.feature_sets_by_stage must be an object")
            elif feature_sets_by_stage:
                valid_feature_sets = set(feature_set_names())
                unknown_stage_names = sorted(set(str(key) for key in feature_sets_by_stage.keys()) - set(_STAGE_NAMES))
                if unknown_stage_names:
                    errors.append(
                        f"{field_prefix}.catalog.feature_sets_by_stage has unknown stages: {unknown_stage_names}"
                    )
                for stage_name, feature_sets in feature_sets_by_stage.items():
                    stage_feature_sets = list(feature_sets or [])
                    if not stage_feature_sets:
                        errors.append(f"{field_prefix}.catalog.feature_sets_by_stage.{stage_name} must not be empty")
                        continue
                    unknown_feature_sets = sorted(set(str(item) for item in stage_feature_sets) - valid_feature_sets)
                    if unknown_feature_sets:
                        errors.append(
                            f"{field_prefix}.catalog.feature_sets_by_stage.{stage_name} has unknown feature sets: "
                            f"{unknown_feature_sets}; valid options: {feature_set_names()}"
                        )

    training = overrides.get("training") or {}
    if training:
        if not isinstance(training, dict):
            errors.append(f"{field_prefix}.training must be an object")
        else:
            unknown_training = sorted(
                set(str(key) for key in training.keys()) - {"search_options_by_stage", "stage2_label_filter", "stage2_session_filter"}
            )
            if unknown_training:
                errors.append(
                    f"{field_prefix}.training supports only search_options_by_stage, stage2_label_filter, and stage2_session_filter; "
                    f"got unsupported keys: {unknown_training}"
                )
            _validate_stage2_label_filter(
                training.get("stage2_label_filter"),
                errors,
                field_prefix=f"{field_prefix}.training.stage2_label_filter",
            )
            _validate_stage2_session_filter(
                training.get("stage2_session_filter"),
                errors,
                field_prefix=f"{field_prefix}.training.stage2_session_filter",
            )
            _validate_search_options_by_stage(
                training.get("search_options_by_stage"),
                errors,
                field_prefix=f"{field_prefix}.training.search_options_by_stage",
            )

    runtime = overrides.get("runtime") or {}
    if runtime:
        if not isinstance(runtime, dict):
            errors.append(f"{field_prefix}.runtime must be an object")
        else:
            unknown_runtime = sorted(set(str(key) for key in runtime.keys()) - {"block_expiry"})
            if unknown_runtime:
                errors.append(
                    f"{field_prefix}.runtime supports only block_expiry; got unsupported keys: {unknown_runtime}"
                )
            if "block_expiry" in runtime and not isinstance(runtime.get("block_expiry"), bool):
                errors.append(f"{field_prefix}.runtime.block_expiry must be boolean")

    outputs = overrides.get("outputs") or {}
    if outputs:
        if not isinstance(outputs, dict):
            errors.append(f"{field_prefix}.outputs must be an object")
        else:
            unknown_outputs = sorted(set(str(key) for key in outputs.keys()) - {"run_name"})
            if unknown_outputs:
                errors.append(
                    f"{field_prefix}.outputs supports only run_name; got unsupported keys: {unknown_outputs}"
                )
            if "run_name" in outputs and not str(outputs.get("run_name") or "").strip():
                errors.append(f"{field_prefix}.outputs.run_name must be non-empty when provided")


def _validate_grid_manifest(payload: Dict[str, Any], *, manifest_path: Path, validate_paths: bool, errors: List[str]) -> Dict[str, Any]:
    manifest_dir = manifest_path.resolve().parent
    inputs_payload = dict(payload.get("inputs") or {})
    outputs_payload = dict(payload.get("outputs") or {})
    selection_payload = dict(payload.get("selection") or {})
    grid_payload = dict(payload.get("grid") or {})

    base_manifest_path = _normalize_path(inputs_payload.get("base_manifest_path"), manifest_dir=manifest_dir)
    artifacts_root = _normalize_path(outputs_payload.get("artifacts_root"), manifest_dir=manifest_dir)
    run_name = str(outputs_payload.get("run_name") or "").strip()

    if base_manifest_path is None:
        errors.append("inputs.base_manifest_path must be set for staged grid manifests")
    elif base_manifest_path.resolve() == manifest_path.resolve():
        errors.append("inputs.base_manifest_path must not point to the grid manifest itself")
    elif not base_manifest_path.exists():
        errors.append(f"inputs.base_manifest_path not found: {base_manifest_path}")

    if artifacts_root is None:
        errors.append("outputs.artifacts_root must be set for staged grid manifests")
    if not run_name:
        errors.append("outputs.run_name must be set for staged grid manifests")

    stage2_hpo_escalation = dict(selection_payload.get("stage2_hpo_escalation") or {})
    if not stage2_hpo_escalation:
        errors.append("selection.stage2_hpo_escalation must be set")
    else:
        for key in ("roc_auc_min", "brier_max"):
            if key not in stage2_hpo_escalation:
                errors.append(f"selection.stage2_hpo_escalation.{key} must be set")
                continue
            try:
                float(stage2_hpo_escalation[key])
            except Exception:
                errors.append(f"selection.stage2_hpo_escalation.{key} must be numeric")
    robustness_probe = dict(selection_payload.get("robustness_probe") or {})
    _validate_grid_robustness_probe(
        robustness_probe,
        errors,
        field_prefix="selection.robustness_probe",
    )

    if grid_payload and not isinstance(grid_payload, dict):
        errors.append("grid must be an object")
        grid_payload = {}
    research_only = grid_payload.get("research_only", True)
    if not isinstance(research_only, bool):
        errors.append("grid.research_only must be boolean")
    max_parallel_runs = grid_payload.get("max_parallel_runs")
    if max_parallel_runs is not None:
        try:
            if int(max_parallel_runs) <= 0:
                raise ValueError
        except Exception:
            errors.append("grid.max_parallel_runs must be an integer > 0 when provided")
    runs_payload = grid_payload.get("runs")
    if runs_payload is None:
        runs = []
    elif not isinstance(runs_payload, list):
        errors.append("grid.runs must be a list")
        runs = []
    else:
        runs = list(runs_payload)
    if not runs:
        errors.append("grid.runs must not be empty")

    seen_run_ids: list[str] = []
    for idx, run_payload in enumerate(runs, start=1):
        field_prefix = f"grid.runs[{idx}]"
        if not isinstance(run_payload, dict):
            errors.append(f"{field_prefix} must be an object")
            continue
        run_id = str(run_payload.get("run_id") or "").strip()
        if not run_id:
            errors.append(f"{field_prefix}.run_id must be non-empty")
        elif run_id in seen_run_ids:
            errors.append(f"{field_prefix}.run_id must be unique; duplicate: {run_id}")
        else:
            seen_run_ids.append(run_id)

        if "model_group_suffix" in run_payload and not isinstance(run_payload.get("model_group_suffix"), str):
            errors.append(f"{field_prefix}.model_group_suffix must be a string when provided")

        inherit_best_from = list(run_payload.get("inherit_best_from") or [])
        if inherit_best_from and not isinstance(run_payload.get("inherit_best_from"), list):
            errors.append(f"{field_prefix}.inherit_best_from must be a list")
        else:
            if len(inherit_best_from) != len(set(str(item) for item in inherit_best_from)):
                errors.append(f"{field_prefix}.inherit_best_from must not contain duplicates")
            unknown_refs = [str(item) for item in inherit_best_from if str(item) not in seen_run_ids]
            if unknown_refs:
                errors.append(
                    f"{field_prefix}.inherit_best_from must reference earlier run_id values; unknown refs: {unknown_refs}"
                )
        reuse_stage1_from = run_payload.get("reuse_stage1_from")
        if reuse_stage1_from is not None and not isinstance(reuse_stage1_from, str):
            errors.append(f"{field_prefix}.reuse_stage1_from must be a string when provided")
        elif isinstance(reuse_stage1_from, str) and reuse_stage1_from.strip():
            if reuse_stage1_from.strip() not in seen_run_ids:
                errors.append(
                    f"{field_prefix}.reuse_stage1_from must reference an earlier run_id; unknown ref: {reuse_stage1_from.strip()}"
                )

        _validate_grid_run_overrides(
            run_payload.get("overrides"),
            errors,
            field_prefix=f"{field_prefix}.overrides",
        )

    base_resolved: Optional[Dict[str, Any]] = None
    if base_manifest_path is not None and base_manifest_path.exists():
        try:
            base_resolved = load_and_resolve_manifest(base_manifest_path, validate_paths=validate_paths)
        except Exception as exc:
            errors.append(f"inputs.base_manifest_path failed to resolve: {exc}")
        else:
            if str(base_resolved.get("experiment_kind") or "") != STAGED_KIND:
                errors.append(f"inputs.base_manifest_path must resolve to experiment_kind={STAGED_KIND}")

    resolved_runs = []
    for run_payload in runs:
        if not isinstance(run_payload, dict):
            continue
        resolved_runs.append(
            {
                "run_id": str(run_payload.get("run_id") or "").strip(),
                "model_group_suffix": str(run_payload.get("model_group_suffix") or ""),
                "inherit_best_from": [str(item) for item in list(run_payload.get("inherit_best_from") or [])],
                "reuse_stage1_from": str(run_payload.get("reuse_stage1_from") or "").strip() or None,
                "overrides": dict(run_payload.get("overrides") or {}),
            }
        )

    resolved: Dict[str, Any] = {
        "schema_version": int(payload.get("schema_version", 0)),
        "experiment_kind": STAGED_GRID_KIND,
        "manifest_path": str(manifest_path.resolve()),
        "inputs": {
            "base_manifest_path": base_manifest_path,
        },
        "outputs": {
            "artifacts_root": artifacts_root,
            "run_name": run_name,
        },
        "selection": {
            "stage2_hpo_escalation": stage2_hpo_escalation,
            "robustness_probe": robustness_probe,
        },
        "grid": {
            "research_only": bool(research_only),
            "max_parallel_runs": (None if max_parallel_runs is None else int(max_parallel_runs)),
            "runs": resolved_runs,
        },
    }
    if base_resolved is not None:
        resolved["base_resolved_manifest"] = base_resolved
        resolved["base_manifest_hash"] = str(base_resolved.get("manifest_hash") or "")
        resolved["base_raw_manifest"] = dict(base_resolved.get("raw_manifest") or {})
    return resolved


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
    kind = str(payload.get("experiment_kind") or "").strip()
    if kind == STAGED_GRID_KIND:
        resolved = _validate_grid_manifest(
            payload,
            manifest_path=manifest_path,
            validate_paths=validate_paths,
            errors=errors,
        )
        if errors:
            raise ManifestValidationError("\n".join(errors))
        return resolved

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
