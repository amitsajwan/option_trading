from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import joblib
import numpy as np
import pandas as pd

from ..contracts.types import LabelRecipe
from ..experiment_control.state import utc_now
from .pipeline import (
    KEY_COLUMNS,
    _apply_runtime_filters,
    _build_oracle_targets,
    _load_dataset,
    _merge_policy_inputs,
    _numeric_array,
    _safe_float,
    _score_single_target,
    _score_stage2_package,
    _stage2_side_masks_from_policy,
    _summarize_returns,
    _window,
)
from .recipes import get_recipe_catalog, recipe_catalog_ids
from .registries import view_registry


DEFAULT_TOP_FRACTIONS: tuple[float, ...] = (1.0, 0.5, 1.0 / 3.0, 0.25, 0.10)
DEFAULT_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _normalize_top_fractions(values: Iterable[float]) -> list[float]:
    normalized: list[float] = []
    seen: set[float] = set()
    for raw in values:
        value = float(raw)
        if not math.isfinite(value) or value <= 0.0 or value > 1.0:
            raise ValueError(f"top fraction must be in (0, 1], got {raw!r}")
        rounded = float(round(value, 6))
        if rounded in seen:
            continue
        normalized.append(rounded)
        seen.add(rounded)
    if not normalized:
        raise ValueError("top fractions must not be empty")
    return normalized


def _recipe_identity(recipe: LabelRecipe) -> tuple[Any, ...]:
    return (
        str(recipe.recipe_id),
        int(recipe.horizon_minutes),
        float(recipe.take_profit_pct),
        float(recipe.stop_loss_pct),
    )


def _resolve_recipe_universe(*, run_recipe_catalog_id: str, fixed_recipe_ids: Sequence[str]) -> list[LabelRecipe]:
    recipes_by_id: dict[str, LabelRecipe] = {}

    def register(recipe: LabelRecipe) -> None:
        recipe_id = str(recipe.recipe_id)
        existing = recipes_by_id.get(recipe_id)
        if existing is None:
            recipes_by_id[recipe_id] = LabelRecipe(**recipe.to_dict())
            return
        if _recipe_identity(existing) != _recipe_identity(recipe):
            raise ValueError(f"recipe_id {recipe_id} is ambiguous across catalogs")

    for recipe in get_recipe_catalog(str(run_recipe_catalog_id)):
        register(recipe)

    requested = {str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()}
    if requested:
        for catalog_id in recipe_catalog_ids():
            for recipe in get_recipe_catalog(catalog_id):
                if str(recipe.recipe_id) in requested:
                    register(recipe)

    missing = sorted(recipe_id for recipe_id in requested if recipe_id not in recipes_by_id)
    if missing:
        raise ValueError(f"unknown fixed recipe ids: {missing}")
    return [recipes_by_id[recipe_id] for recipe_id in sorted(recipes_by_id)]


def _subset_size(total: int, fraction: float) -> int:
    if total <= 0:
        return 0
    if fraction >= 1.0:
        return int(total)
    return max(1, int(math.ceil(total * float(fraction))))


def _summary_with_recipe(
    returns: Sequence[float],
    *,
    rows_total: int,
    sides: Sequence[str],
    recipe_id: str,
) -> dict[str, Any]:
    summary = _summarize_returns(
        returns,
        rows_total=rows_total,
        sides=sides,
        selected_recipes=[str(recipe_id)] * len(list(returns)),
    )
    summary["recipe_id"] = str(recipe_id)
    return summary


def _json_ready_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, np.generic):
            out[key] = value.item()
        elif isinstance(value, (list, tuple)):
            out[key] = [item.item() if isinstance(item, np.generic) else item for item in value]
        else:
            out[key] = value
    return out


def analyze_stage12_counterfactual(
    *,
    run_dir: str | Path,
    top_fractions: Sequence[float] = DEFAULT_TOP_FRACTIONS,
    fixed_recipe_ids: Sequence[str] = DEFAULT_FIXED_RECIPE_IDS,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary_path = source_run_dir / "summary.json"
    resolved_config_path = source_run_dir / "resolved_config.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary.json not found: {summary_path}")
    if not resolved_config_path.exists():
        raise FileNotFoundError(f"resolved_config.json not found: {resolved_config_path}")

    summary = _load_json(summary_path)
    resolved_config = _load_json(resolved_config_path)
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    stage1_policy = dict((summary.get("policy_reports") or {}).get("stage1") or {})
    stage2_policy = dict((summary.get("policy_reports") or {}).get("stage2") or {})
    if "selected_threshold" not in stage1_policy:
        raise ValueError("stage1 selected_threshold missing from summary policy_reports")
    if "selected_ce_threshold" not in stage2_policy or "selected_pe_threshold" not in stage2_policy:
        raise ValueError("stage2 thresholds missing from summary policy_reports")

    normalized_fractions = _normalize_top_fractions(top_fractions)
    normalized_fixed_recipe_ids = [str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()]
    if not normalized_fixed_recipe_ids:
        raise ValueError("fixed_recipe_ids must not be empty")

    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    if not support_dataset:
        raise ValueError("resolved_config.inputs.support_dataset is required")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))

    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"counterfactual support dataset {support_dataset}",
    )
    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=normalized_fixed_recipe_ids,
    )
    _, utility = _build_oracle_targets(
        support_filtered,
        recipe_universe,
        cost_per_trade=float(((resolved_config.get("training") or {}).get("cost_per_trade") or 0.0)),
    )
    utility_holdout = _window(utility, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))
    rows_total = int(len(utility_holdout))

    component_ids = dict(summary.get("component_ids") or {})
    stage1_view_id = str(((component_ids.get("stage1") or {}).get("view_id")) or "")
    stage2_view_id = str(((component_ids.get("stage2") or {}).get("view_id")) or "")
    if not stage1_view_id or not stage2_view_id:
        raise ValueError("summary component_ids must include stage1/stage2 view_id values")

    stage1_dataset_name = view_registry()[stage1_view_id].dataset_name
    stage2_dataset_name = view_registry()[stage2_view_id].dataset_name
    stage1_frame, _ = _apply_runtime_filters(
        _load_dataset(parquet_root, stage1_dataset_name),
        block_expiry=runtime_block_expiry,
        context=f"counterfactual stage1 dataset {stage1_dataset_name}",
        support_context=support_context,
    )
    stage2_frame, _ = _apply_runtime_filters(
        _load_dataset(parquet_root, stage2_dataset_name),
        block_expiry=runtime_block_expiry,
        context=f"counterfactual stage2 dataset {stage2_dataset_name}",
        support_context=support_context,
    )
    stage1_holdout = _window(stage1_frame, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))
    stage2_holdout = _window(stage2_frame, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package = joblib.load(str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or ""))
    stage2_package = joblib.load(str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or ""))
    if not isinstance(stage1_package, dict) or not isinstance(stage2_package, dict):
        raise ValueError("stage1/stage2 model packages must be dictionaries")

    stage1_scores = _score_single_target(stage1_holdout, stage1_package, prob_col="entry_prob")
    stage2_scores = _score_stage2_package(stage2_holdout, stage2_package)

    merged = _merge_policy_inputs(utility_holdout, stage1_scores, stage2_scores)
    direction_available = pd.to_numeric(merged["direction_up_prob"], errors="coerce").notna().to_numpy(dtype=bool, copy=False)
    ce_mask, pe_mask = _stage2_side_masks_from_policy(
        merged,
        entry_threshold=float(stage1_policy["selected_threshold"]),
        stage2_policy=stage2_policy,
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    trade_mask = ce_mask | pe_mask

    selected = merged.loc[trade_mask].copy()
    if len(selected) == 0:
        raise ValueError("no Stage1+Stage2 trades were selected on holdout")

    selected["selected_side"] = np.where(ce_mask[trade_mask], "CE", "PE")
    selected["entry_prob"] = pd.to_numeric(selected["entry_prob"], errors="coerce").fillna(0.0)
    if "direction_trade_prob" in selected.columns:
        selected["direction_trade_prob"] = pd.to_numeric(selected["direction_trade_prob"], errors="coerce").fillna(1.0)
    else:
        selected["direction_trade_prob"] = 1.0
    selected["direction_up_prob"] = pd.to_numeric(selected["direction_up_prob"], errors="coerce").fillna(0.5)
    selected["selected_side_prob"] = np.where(
        selected["selected_side"].eq("CE"),
        selected["direction_up_prob"],
        1.0 - selected["direction_up_prob"],
    )
    selected["direction_margin"] = (selected["selected_side_prob"] - 0.5).clip(lower=0.0) * 2.0
    selected["ranking_score"] = (
        selected["entry_prob"]
        * selected["direction_trade_prob"]
        * selected["selected_side_prob"]
    )
    selected["oracle_selected_side_return"] = np.where(
        selected["selected_side"].eq("CE"),
        pd.to_numeric(selected["best_ce_net_return_after_cost"], errors="coerce").fillna(0.0),
        pd.to_numeric(selected["best_pe_net_return_after_cost"], errors="coerce").fillna(0.0),
    )
    for recipe in recipe_universe:
        recipe_id = str(recipe.recipe_id)
        ce_col = f"{recipe_id}__ce_net_return"
        pe_col = f"{recipe_id}__pe_net_return"
        if ce_col not in selected.columns or pe_col not in selected.columns:
            continue
        selected[f"{recipe_id}__selected_return"] = np.where(
            selected["selected_side"].eq("CE"),
            pd.to_numeric(selected[ce_col], errors="coerce").fillna(0.0),
            pd.to_numeric(selected[pe_col], errors="coerce").fillna(0.0),
        )

    selected = selected.sort_values(
        by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)

    results: list[dict[str, Any]] = []
    selected_trade_count = int(len(selected))
    for fraction in normalized_fractions:
        keep_count = _subset_size(selected_trade_count, fraction)
        subset = selected.head(keep_count).copy()
        sides = subset["selected_side"].astype(str).tolist()
        oracle_summary = _summary_with_recipe(
            subset["oracle_selected_side_return"].astype(float).tolist(),
            rows_total=rows_total,
            sides=sides,
            recipe_id="ORACLE_SELECTED_SIDE",
        )
        fixed_summaries: dict[str, dict[str, Any]] = {}
        for recipe_id in normalized_fixed_recipe_ids:
            return_col = f"{recipe_id}__selected_return"
            if return_col not in subset.columns:
                raise ValueError(f"counterfactual return column missing for recipe_id={recipe_id}")
            fixed_summaries[recipe_id] = _summary_with_recipe(
                subset[return_col].astype(float).tolist(),
                rows_total=rows_total,
                sides=sides,
                recipe_id=recipe_id,
            )
        results.append(
            {
                "fraction": float(fraction),
                "selected_trades": int(len(subset)),
                "min_ranking_score": _safe_float(subset["ranking_score"].iloc[-1], default=0.0) if len(subset) else 0.0,
                "max_ranking_score": _safe_float(subset["ranking_score"].iloc[0], default=0.0) if len(subset) else 0.0,
                "oracle_selected_side": _json_ready_summary(oracle_summary),
                "fixed_recipes": {recipe_id: _json_ready_summary(summary) for recipe_id, summary in fixed_summaries.items()},
            }
        )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage12_counterfactual")
    analysis_root.mkdir(parents=True, exist_ok=True)
    ranked_trades_path = analysis_root / "ranked_trades.parquet"
    summary_output_path = analysis_root / "analysis_summary.json"

    preview_columns = [
        *KEY_COLUMNS,
        "selected_side",
        "entry_prob",
        "direction_trade_prob",
        "direction_up_prob",
        "selected_side_prob",
        "direction_margin",
        "ranking_score",
        "oracle_selected_side_return",
        *[f"{recipe_id}__selected_return" for recipe_id in normalized_fixed_recipe_ids if f"{recipe_id}__selected_return" in selected.columns],
    ]
    selected.loc[:, [column for column in preview_columns if column in selected.columns]].to_parquet(ranked_trades_path, index=False)

    payload = {
        "analysis_kind": "stage12_counterfactual_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "rows_total": rows_total,
        "selected_trade_count": selected_trade_count,
        "ranking": {
            "score_id": "entry_prob_x_trade_gate_prob_x_selected_side_prob_v1",
            "top_fractions": normalized_fractions,
        },
        "stage1_policy": {
            "policy_id": str(stage1_policy.get("policy_id") or ""),
            "selected_threshold": float(stage1_policy["selected_threshold"]),
        },
        "stage2_policy": {
            key: value
            for key, value in stage2_policy.items()
            if key.startswith("selected_") or key == "policy_id"
        },
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "fixed_recipe_ids": normalized_fixed_recipe_ids,
        "results": results,
        "top_trade_preview": selected.loc[:, [column for column in preview_columns if column in selected.columns]].head(10).to_dict(orient="records"),
        "paths": {
            "analysis_root": str(analysis_root),
            "ranked_trades": str(ranked_trades_path),
            "analysis_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_FIXED_RECIPE_IDS",
    "DEFAULT_TOP_FRACTIONS",
    "analyze_stage12_counterfactual",
]
