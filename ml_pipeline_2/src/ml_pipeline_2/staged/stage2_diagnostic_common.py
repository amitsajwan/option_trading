from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

import joblib
import pandas as pd

from .counterfactual import _load_json, _resolve_recipe_universe
from .pipeline import (
    KEY_COLUMNS,
    _apply_runtime_filters,
    _build_oracle_targets,
    _load_dataset,
    _merge_policy_inputs,
    _score_single_target,
    _score_stage2_package,
    _window,
)
from .registries import view_registry
from .skew_diagnostic import _drop_base_overlap


@dataclass(frozen=True)
class Stage2DiagnosticContext:
    source_run_dir: Path
    source_run_id: str
    summary: Dict[str, Any]
    resolved_config: Dict[str, Any]
    fixed_recipe_ids: tuple[str, ...]
    recipe_universe: Sequence[Any]
    parquet_root: Path
    support_context: pd.DataFrame
    runtime_block_expiry: bool
    diagnostic_windows: Dict[str, pd.DataFrame]
    stage1_package: Dict[str, Any]
    stage2_package: Dict[str, Any]
    stage1_policy: Dict[str, Any]
    stage2_policy: Dict[str, Any]
    stage1_filtered: pd.DataFrame
    stage2_filtered: pd.DataFrame


def _normalize_fixed_recipe_ids(fixed_recipe_ids: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip())
    if not normalized:
        raise ValueError("fixed_recipe_ids must not be empty")
    return normalized


def load_stage2_diagnostic_context(
    *,
    run_dir: str | Path,
    fixed_recipe_ids: Sequence[str],
    context_label: str,
) -> Stage2DiagnosticContext:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    normalized_fixed_recipe_ids = _normalize_fixed_recipe_ids(fixed_recipe_ids)
    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))

    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"{context_label} support dataset {support_dataset}",
    )

    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=normalized_fixed_recipe_ids,
    )
    oracle, utility = _build_oracle_targets(
        support_filtered,
        recipe_universe,
        cost_per_trade=float(((resolved_config.get("training") or {}).get("cost_per_trade") or 0.0)),
    )
    utility_dupes = [c for c in utility.columns if c in set(oracle.columns) - {"trade_date", "timestamp", "snapshot_id"}]
    utility_base = utility.drop(columns=utility_dupes) if utility_dupes else utility
    diagnostic_base = _merge_policy_inputs(oracle, utility_base)

    component_ids = dict(summary.get("component_ids") or {})
    stage1_view_id = str(((component_ids.get("stage1") or {}).get("view_id")) or "")
    stage2_view_id = str(((component_ids.get("stage2") or {}).get("view_id")) or "")
    stage1_dataset = view_registry()[stage1_view_id].dataset_name
    stage2_dataset = view_registry()[stage2_view_id].dataset_name

    stage1_raw = _load_dataset(parquet_root, stage1_dataset)
    stage2_raw = _load_dataset(parquet_root, stage2_dataset)
    stage1_filtered, _ = _apply_runtime_filters(
        stage1_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context=f"{context_label} stage1",
    )
    stage2_filtered, _ = _apply_runtime_filters(
        stage2_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context=f"{context_label} stage2",
    )

    diagnostic_windows = {
        window_name: _window(diagnostic_base, dict((resolved_config.get("windows") or {}).get(window_name) or {}))
        for window_name in ("research_valid", "final_holdout")
    }

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package = joblib.load(str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or ""))
    stage2_package = joblib.load(str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or ""))

    return Stage2DiagnosticContext(
        source_run_dir=source_run_dir,
        source_run_id=str(summary.get("run_id") or source_run_dir.name),
        summary=summary,
        resolved_config=resolved_config,
        fixed_recipe_ids=normalized_fixed_recipe_ids,
        recipe_universe=recipe_universe,
        parquet_root=parquet_root,
        support_context=support_context,
        runtime_block_expiry=runtime_block_expiry,
        diagnostic_windows=diagnostic_windows,
        stage1_package=stage1_package,
        stage2_package=stage2_package,
        stage1_policy=dict((summary.get("policy_reports") or {}).get("stage1") or {}),
        stage2_policy=dict((summary.get("policy_reports") or {}).get("stage2") or {}),
        stage1_filtered=stage1_filtered,
        stage2_filtered=stage2_filtered,
    )


def build_stage2_scored_window_frame(
    context: Stage2DiagnosticContext,
    *,
    window_name: str,
    include_stage2_feature_columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    diagnostic_window = context.diagnostic_windows[str(window_name)]
    window_cfg = dict((context.resolved_config.get("windows") or {}).get(window_name) or {})
    stage1_window = _window(context.stage1_filtered, window_cfg)
    stage2_window = _window(context.stage2_filtered, window_cfg)
    stage1_scores = _drop_base_overlap(
        _score_single_target(stage1_window, context.stage1_package, prob_col="entry_prob"),
        diagnostic_window.columns,
    )
    stage2_scores = _drop_base_overlap(
        _score_stage2_package(stage2_window, context.stage2_package),
        diagnostic_window.columns,
    )

    merges: list[pd.DataFrame] = [diagnostic_window, stage1_scores, stage2_scores]
    feature_columns = [str(col) for col in list(include_stage2_feature_columns or []) if str(col) in stage2_window.columns]
    if feature_columns:
        stage2_features = _drop_base_overlap(
            stage2_window.loc[:, KEY_COLUMNS + feature_columns],
            list(diagnostic_window.columns) + list(stage1_scores.columns) + list(stage2_scores.columns),
        )
        merges.append(stage2_features)
    return _merge_policy_inputs(*merges)


__all__ = [
    "Stage2DiagnosticContext",
    "build_stage2_scored_window_frame",
    "load_stage2_diagnostic_context",
]
