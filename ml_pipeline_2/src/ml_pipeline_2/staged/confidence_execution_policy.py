from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Sequence

import numpy as np
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


DEFAULT_CONFIDENCE_EXECUTION_POLICY_TOP_FRACTIONS: tuple[float, ...] = (0.5, 1.0 / 3.0, 0.25)
DEFAULT_CONFIDENCE_EXECUTION_POLICY_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")
DEFAULT_CONFIDENCE_EXECUTION_POLICY_SIDE_CAPS: tuple[float, ...] = (1.0, 0.85, 0.75, 0.70)
DEFAULT_CONFIDENCE_EXECUTION_POLICY_CONFIG: dict[str, Any] = {
    "validation_min_trades_soft": 50,
    "side_share_min": 0.30,
    "side_share_max": 0.70,
    "prefer_non_negative_returns": True,
    "prefer_profit_factor_min": 1.0,
}


def _normalize_side_caps(side_cap_grid: Sequence[float]) -> list[float]:
    seen: set[float] = set()
    normalized: list[float] = []
    for raw in side_cap_grid:
        value = float(raw)
        if not np.isfinite(value) or value <= 0.5 or value > 1.0:
            raise ValueError("side_cap_grid values must be finite and within (0.5, 1.0]")
        rounded = round(value, 6)
        if rounded in seen:
            continue
        seen.add(rounded)
        normalized.append(float(rounded))
    if not normalized:
        raise ValueError("side_cap_grid must not be empty")
    return normalized


def _apply_side_cap(subset: pd.DataFrame, side_cap: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    if len(subset) == 0:
        return subset.copy(), {"side_cap_max": float(side_cap), "trimmed": False, "keep_ce": 0, "keep_pe": 0}
    if float(side_cap) >= 0.999999:
        ce_count = int(subset["selected_side"].eq("CE").sum())
        pe_count = int(subset["selected_side"].eq("PE").sum())
        return subset.copy(), {"side_cap_max": float(side_cap), "trimmed": False, "keep_ce": ce_count, "keep_pe": pe_count}

    ce = subset.loc[subset["selected_side"].eq("CE")].copy()
    pe = subset.loc[subset["selected_side"].eq("PE")].copy()
    ce_count = int(len(ce))
    pe_count = int(len(pe))
    total = ce_count + pe_count
    if total <= 0:
        return subset.iloc[0:0].copy(), {"side_cap_max": float(side_cap), "trimmed": False, "keep_ce": 0, "keep_pe": 0}

    long_share = float(ce_count / total)
    keep_ce = ce_count
    keep_pe = pe_count
    trimmed = False

    if long_share > float(side_cap):
        keep_ce = min(ce_count, max(0, int(np.floor((float(side_cap) * pe_count) / max(1e-9, 1.0 - float(side_cap))))))
        trimmed = keep_ce < ce_count
    elif long_share < float(1.0 - side_cap):
        keep_pe = min(pe_count, max(0, int(np.floor((float(side_cap) * ce_count) / max(1e-9, 1.0 - float(side_cap))))))
        trimmed = keep_pe < pe_count

    capped = pd.concat([ce.iloc[:keep_ce].copy(), pe.iloc[:keep_pe].copy()], ignore_index=True)
    if len(capped):
        capped = capped.sort_values(
            by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
            ascending=[False, False, False, True],
        ).reset_index(drop=True)
    return capped, {
        "side_cap_max": float(side_cap),
        "trimmed": bool(trimmed),
        "keep_ce": int(keep_ce),
        "keep_pe": int(keep_pe),
    }


def run_stage12_confidence_execution_policy(
    *,
    run_dir: str | Path,
    top_fractions: Sequence[float] = DEFAULT_CONFIDENCE_EXECUTION_POLICY_TOP_FRACTIONS,
    fixed_recipe_ids: Sequence[str] = DEFAULT_CONFIDENCE_EXECUTION_POLICY_FIXED_RECIPE_IDS,
    side_cap_grid: Sequence[float] = DEFAULT_CONFIDENCE_EXECUTION_POLICY_SIDE_CAPS,
    validation_policy: Dict[str, Any] | None = None,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    normalized_fractions = _normalize_top_fractions(top_fractions)
    normalized_fixed_recipe_ids = [str(recipe_id).strip() for recipe_id in fixed_recipe_ids if str(recipe_id).strip()]
    if not normalized_fixed_recipe_ids:
        raise ValueError("fixed_recipe_ids must not be empty")
    normalized_side_caps = _normalize_side_caps(side_cap_grid)
    policy_config = {
        **DEFAULT_CONFIDENCE_EXECUTION_POLICY_CONFIG,
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
        context=f"confidence execution policy support dataset {support_dataset}",
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

    import joblib

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

    for fraction in normalized_fractions:
        valid_fraction_subset, valid_keep_count, valid_score_floor = _top_fraction_subset(selected_valid, fraction)
        holdout_fraction_subset, holdout_keep_count, holdout_score_floor = _top_fraction_subset(selected_holdout, fraction)
        for side_cap in normalized_side_caps:
            valid_subset, valid_cap_meta = _apply_side_cap(valid_fraction_subset, side_cap)
            holdout_subset, holdout_cap_meta = _apply_side_cap(holdout_fraction_subset, side_cap)
            oracle_valid = _oracle_summary(valid_subset, rows_total=valid_rows_total, fraction=fraction)
            oracle_holdout = _oracle_summary(holdout_subset, rows_total=holdout_rows_total, fraction=fraction)
            for recipe_id in normalized_fixed_recipe_ids:
                valid_summary = _apply_policy_soft_preferences(
                    _candidate_summary(valid_subset, rows_total=valid_rows_total, recipe_id=recipe_id, fraction=fraction),
                    policy_config,
                )
                holdout_summary = _apply_policy_soft_preferences(
                    _candidate_summary(holdout_subset, rows_total=holdout_rows_total, recipe_id=recipe_id, fraction=fraction),
                    policy_config,
                )
                rows.append(
                    {
                        "recipe_id": str(recipe_id),
                        "fraction": float(fraction),
                        "side_cap_max": float(side_cap),
                        "validation_keep_count": int(valid_keep_count),
                        "holdout_keep_count": int(holdout_keep_count),
                        "validation_score_floor": float(valid_score_floor),
                        "holdout_score_floor": float(holdout_score_floor),
                        "validation_cap": valid_cap_meta,
                        "holdout_cap": holdout_cap_meta,
                        "validation": valid_summary,
                        "holdout": holdout_summary,
                        "oracle_validation": oracle_valid,
                        "oracle_holdout": oracle_holdout,
                    }
                )

    best = max(
        rows,
        key=lambda row: _economic_balance_rank(
            dict(row["validation"]),
            policy_config,
            -float(row["side_cap_max"]),
            float(row["fraction"]),
        ),
    )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage12_confidence_execution_policy")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "execution_policy_summary.json"

    payload = {
        "analysis_kind": "stage12_confidence_execution_policy_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "ranking": {
            "score_id": "entry_prob_x_trade_gate_prob_x_selected_side_prob_v1",
            "top_fractions": normalized_fractions,
            "side_cap_grid": normalized_side_caps,
            "transfer_mode": "fraction",
        },
        "validation_policy": dict(policy_config),
        "selected_trade_count": {
            "research_valid": int(len(selected_valid)),
            "final_holdout": int(len(selected_holdout)),
        },
        "fixed_recipe_ids": normalized_fixed_recipe_ids,
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "winner": best,
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "execution_policy_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_CONFIDENCE_EXECUTION_POLICY_CONFIG",
    "DEFAULT_CONFIDENCE_EXECUTION_POLICY_FIXED_RECIPE_IDS",
    "DEFAULT_CONFIDENCE_EXECUTION_POLICY_SIDE_CAPS",
    "DEFAULT_CONFIDENCE_EXECUTION_POLICY_TOP_FRACTIONS",
    "run_stage12_confidence_execution_policy",
]
