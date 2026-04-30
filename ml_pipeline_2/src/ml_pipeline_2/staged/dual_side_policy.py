from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Sequence

import joblib
import pandas as pd

from ..experiment_control.state import utc_now
from .confidence_execution import (
    _attach_recipe_selected_returns,
    _candidate_summary,
    _oracle_summary,
    _selected_stage12_trades_for_window,
    _top_fraction_subset,
)
from .counterfactual import _load_json, _normalize_top_fractions, _resolve_recipe_universe
from .pipeline import (
    _apply_policy_soft_preferences,
    _apply_runtime_filters,
    _build_oracle_targets,
    _economic_balance_rank,
    _load_dataset,
    _window,
)


DEFAULT_DUAL_SIDE_POLICY_CE_FRACTIONS: tuple[float, ...] = (1.0, 0.5, 1.0 / 3.0, 0.25)
DEFAULT_DUAL_SIDE_POLICY_PE_FRACTIONS: tuple[float, ...] = (1.0, 0.5, 1.0 / 3.0, 0.25)
DEFAULT_DUAL_SIDE_POLICY_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")
DEFAULT_DUAL_SIDE_POLICY_CONFIG: dict[str, Any] = {
    "validation_min_trades_soft": 50,
    "side_share_min": 0.30,
    "side_share_max": 0.70,
    "prefer_non_negative_returns": True,
    "prefer_profit_factor_min": 1.0,
}


def _select_side_fraction(frame: pd.DataFrame, side: str, fraction: float) -> tuple[pd.DataFrame, int, float]:
    side_frame = frame.loc[frame["selected_side"].eq(str(side))].copy().reset_index(drop=True)
    subset, keep_count, score_floor = _top_fraction_subset(side_frame, fraction)
    return subset, int(keep_count), float(score_floor)


def _dual_side_subset(frame: pd.DataFrame, *, ce_fraction: float, pe_fraction: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    ce_subset, ce_keep_count, ce_score_floor = _select_side_fraction(frame, "CE", ce_fraction)
    pe_subset, pe_keep_count, pe_score_floor = _select_side_fraction(frame, "PE", pe_fraction)
    combined = pd.concat([ce_subset, pe_subset], ignore_index=True)
    if len(combined):
        combined = combined.sort_values(
            by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
            ascending=[False, False, False, True],
        ).reset_index(drop=True)
    return combined, {
        "ce_fraction": float(ce_fraction),
        "pe_fraction": float(pe_fraction),
        "ce_keep_count": int(ce_keep_count),
        "pe_keep_count": int(pe_keep_count),
        "ce_score_floor": None if ce_score_floor == float("-inf") else float(ce_score_floor),
        "pe_score_floor": None if pe_score_floor == float("-inf") else float(pe_score_floor),
    }


def run_stage12_dual_side_policy(
    *,
    run_dir: str | Path,
    ce_fraction_grid: Sequence[float] = DEFAULT_DUAL_SIDE_POLICY_CE_FRACTIONS,
    pe_fraction_grid: Sequence[float] = DEFAULT_DUAL_SIDE_POLICY_PE_FRACTIONS,
    fixed_recipe_ids: Sequence[str] = DEFAULT_DUAL_SIDE_POLICY_FIXED_RECIPE_IDS,
    validation_policy: Dict[str, Any] | None = None,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    normalized_ce_fractions = _normalize_top_fractions(ce_fraction_grid)
    normalized_pe_fractions = _normalize_top_fractions(pe_fraction_grid)
    normalized_fixed_recipe_ids = [str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()]
    if not normalized_fixed_recipe_ids:
        raise ValueError("fixed_recipe_ids must not be empty")
    policy_config = {
        **DEFAULT_DUAL_SIDE_POLICY_CONFIG,
        **dict(validation_policy or {}),
    }

    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))
    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"dual-side policy support dataset {support_dataset}",
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
    utility_valid = _window(utility, dict((resolved_config.get("windows") or {}).get("research_valid") or {}))
    utility_holdout = _window(utility, dict((resolved_config.get("windows") or {}).get("final_holdout") or {}))

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package_path = str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or "")
    stage2_package_path = str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or "")
    if not stage1_package_path or not stage2_package_path:
        raise ValueError("stage1/stage2 model package paths are required")

    stage1_package = joblib.load(stage1_package_path)
    stage2_package = joblib.load(stage2_package_path)

    selected_valid = _attach_recipe_selected_returns(
        _selected_stage12_trades_for_window(
            resolved_config=resolved_config,
            summary=summary,
            window_name="research_valid",
            utility_window=utility_valid,
            support_context=support_context,
            parquet_root=parquet_root,
            runtime_block_expiry=runtime_block_expiry,
            stage1_package=stage1_package,
            stage2_package=stage2_package,
        ),
        recipe_universe,
    )
    selected_holdout = _attach_recipe_selected_returns(
        _selected_stage12_trades_for_window(
            resolved_config=resolved_config,
            summary=summary,
            window_name="final_holdout",
            utility_window=utility_holdout,
            support_context=support_context,
            parquet_root=parquet_root,
            runtime_block_expiry=runtime_block_expiry,
            stage1_package=stage1_package,
            stage2_package=stage2_package,
        ),
        recipe_universe,
    )

    if len(selected_valid) == 0:
        raise ValueError("no validation trades were selected after Stage1+Stage2 policy")

    valid_rows_total = int(len(utility_valid))
    holdout_rows_total = int(len(utility_holdout))
    rows: list[dict[str, Any]] = []

    for ce_fraction in normalized_ce_fractions:
        for pe_fraction in normalized_pe_fractions:
            valid_subset, valid_meta = _dual_side_subset(selected_valid, ce_fraction=ce_fraction, pe_fraction=pe_fraction)
            holdout_subset, holdout_meta = _dual_side_subset(selected_holdout, ce_fraction=ce_fraction, pe_fraction=pe_fraction)
            oracle_valid = _oracle_summary(valid_subset, rows_total=valid_rows_total, fraction=1.0)
            oracle_holdout = _oracle_summary(holdout_subset, rows_total=holdout_rows_total, fraction=1.0)
            for recipe_id in normalized_fixed_recipe_ids:
                valid_summary = _apply_policy_soft_preferences(
                    _candidate_summary(valid_subset, rows_total=valid_rows_total, recipe_id=recipe_id, fraction=1.0),
                    policy_config,
                )
                holdout_summary = _apply_policy_soft_preferences(
                    _candidate_summary(holdout_subset, rows_total=holdout_rows_total, recipe_id=recipe_id, fraction=1.0),
                    policy_config,
                )
                rows.append(
                    {
                        "recipe_id": str(recipe_id),
                        "ce_fraction": float(ce_fraction),
                        "pe_fraction": float(pe_fraction),
                        "validation_selection": dict(valid_meta),
                        "holdout_selection": dict(holdout_meta),
                        "validation": valid_summary,
                        "holdout": holdout_summary,
                        "oracle_validation": oracle_valid,
                        "oracle_holdout": oracle_holdout,
                    }
                )

    winner = max(
        rows,
        key=lambda row: _economic_balance_rank(
            dict(row["validation"]),
            policy_config,
            -abs(float(row["ce_fraction"]) - float(row["pe_fraction"])),
            float(row["ce_fraction"]),
            float(row["pe_fraction"]),
        ),
    )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage12_dual_side_policy")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "dual_side_policy_summary.json"

    payload = {
        "analysis_kind": "stage12_dual_side_policy_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "ranking": {
            "score_id": "entry_prob_x_trade_gate_prob_x_selected_side_prob_v1",
            "ce_fraction_grid": normalized_ce_fractions,
            "pe_fraction_grid": normalized_pe_fractions,
            "selection_mode": "independent_side_fractions",
        },
        "validation_policy": dict(policy_config),
        "selected_trade_count": {
            "research_valid": int(len(selected_valid)),
            "final_holdout": int(len(selected_holdout)),
        },
        "fixed_recipe_ids": normalized_fixed_recipe_ids,
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "winner": winner,
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "dual_side_policy_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_DUAL_SIDE_POLICY_CE_FRACTIONS",
    "DEFAULT_DUAL_SIDE_POLICY_FIXED_RECIPE_IDS",
    "DEFAULT_DUAL_SIDE_POLICY_PE_FRACTIONS",
    "DEFAULT_DUAL_SIDE_POLICY_CONFIG",
    "run_stage12_dual_side_policy",
]
