from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence

import numpy as np
import pandas as pd

from .confidence_execution import _attach_recipe_selected_returns, _candidate_summary, _oracle_summary
from .pipeline import _apply_policy_soft_preferences, _economic_balance_rank, _stage2_side_masks_from_policy
from .skew_diagnostic import _model_level_summary


DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")
DEFAULT_STAGE2_CALIBRATION_POLICY: dict[str, Any] = {
    "validation_min_trades_soft": 50,
    "side_share_min": 0.30,
    "side_share_max": 0.70,
    "prefer_non_negative_returns": True,
    "prefer_profit_factor_min": 1.0,
}


def _normalize_grid(values: Iterable[float], *, minimum: float = 0.0, maximum: float = 1.0) -> list[float]:
    seen: set[float] = set()
    normalized: list[float] = []
    for raw in values:
        value = round(float(raw), 6)
        if not np.isfinite(value) or value < minimum or value > maximum:
            raise ValueError(f"grid values must be finite and within [{minimum}, {maximum}]")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(float(value))
    if not normalized:
        raise ValueError("grid must not be empty")
    return sorted(normalized)


def _candidate_stage2_policy(current_policy_id: str, *, trade_threshold: float | None, ce_threshold: float, pe_threshold: float, min_edge: float) -> dict[str, Any]:
    out = {
        "policy_id": str(current_policy_id),
        "selected_ce_threshold": float(ce_threshold),
        "selected_pe_threshold": float(pe_threshold),
        "selected_min_edge": float(min_edge),
    }
    if current_policy_id in {"direction_gate_threshold_v1", "direction_gate_economic_balance_v1"}:
        out["selected_trade_threshold"] = float(trade_threshold if trade_threshold is not None else 0.0)
    return out


def _is_current_stage2_policy(
    current_policy: Dict[str, Any],
    *,
    uses_trade_gate: bool,
    trade_threshold: float | None,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
) -> bool:
    if abs(float(current_policy.get("selected_ce_threshold", -1.0)) - float(ce_threshold)) >= 1e-9:
        return False
    if abs(float(current_policy.get("selected_pe_threshold", -1.0)) - float(pe_threshold)) >= 1e-9:
        return False
    if abs(float(current_policy.get("selected_min_edge", -1.0)) - float(min_edge)) >= 1e-9:
        return False
    if not uses_trade_gate:
        return True
    return abs(float(current_policy.get("selected_trade_threshold", -1.0)) - float(trade_threshold or 0.0)) < 1e-9


def _stage2_selected_frame(
    merged: pd.DataFrame,
    *,
    entry_threshold: float,
    stage2_policy: Dict[str, Any],
    recipe_universe: Sequence[Any],
) -> pd.DataFrame:
    direction_available = pd.to_numeric(
        merged["direction_up_prob"] if "direction_up_prob" in merged.columns else pd.Series([float("nan")] * len(merged), index=merged.index),
        errors="coerce",
    ).notna().to_numpy(dtype=bool, copy=False)
    ce_mask, pe_mask = _stage2_side_masks_from_policy(
        merged,
        entry_threshold=float(entry_threshold),
        stage2_policy=stage2_policy,
    )
    ce_mask = ce_mask & direction_available
    pe_mask = pe_mask & direction_available
    trade_mask = ce_mask | pe_mask
    selected = merged.loc[trade_mask].copy()
    if len(selected) == 0:
        return selected

    selected["selected_side"] = np.where(ce_mask[trade_mask], "CE", "PE")
    selected["entry_prob"] = pd.to_numeric(selected["entry_prob"], errors="coerce").fillna(0.0)
    selected["direction_trade_prob"] = pd.to_numeric(
        selected["direction_trade_prob"] if "direction_trade_prob" in selected.columns else pd.Series([1.0] * len(selected), index=selected.index),
        errors="coerce",
    ).fillna(1.0)
    selected["direction_up_prob"] = pd.to_numeric(selected["direction_up_prob"], errors="coerce").fillna(0.5)
    selected["selected_side_prob"] = np.where(
        selected["selected_side"].eq("CE"),
        selected["direction_up_prob"],
        1.0 - selected["direction_up_prob"],
    )
    selected["ranking_score"] = selected["entry_prob"] * selected["direction_trade_prob"] * selected["selected_side_prob"]
    selected["oracle_selected_side_return"] = np.where(
        selected["selected_side"].eq("CE"),
        pd.to_numeric(selected["best_ce_net_return_after_cost"], errors="coerce").fillna(0.0),
        pd.to_numeric(selected["best_pe_net_return_after_cost"], errors="coerce").fillna(0.0),
    )
    selected = _attach_recipe_selected_returns(selected, recipe_universe)
    return selected.sort_values(
        by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)


def _evaluate_window(
    selected: pd.DataFrame,
    *,
    rows_total: int,
    fixed_recipe_ids: Sequence[str],
    policy_config: Dict[str, Any],
) -> dict[str, Any]:
    actionable_ce = int(selected["selected_side"].eq("CE").sum()) if "selected_side" in selected.columns else 0
    actionable_pe = int(selected["selected_side"].eq("PE").sum()) if "selected_side" in selected.columns else 0
    actionable = _model_level_summary(selected, rows_total, actionable_ce, actionable_pe)
    oracle_summary = _oracle_summary(selected, rows_total=rows_total, fraction=1.0)
    fixed_recipe_summaries: dict[str, Any] = {}
    for recipe_id in fixed_recipe_ids:
        fixed_recipe_summaries[str(recipe_id)] = _apply_policy_soft_preferences(
            _candidate_summary(selected, rows_total=rows_total, recipe_id=str(recipe_id), fraction=1.0),
            policy_config,
        )
    return {
        "actionable": actionable,
        "oracle_selected_side": oracle_summary,
        "fixed_recipe_summaries": fixed_recipe_summaries,
    }


def _best_recipe_id(window_eval: Dict[str, Any], *, policy_config: Dict[str, Any]) -> str:
    summaries = dict(window_eval.get("fixed_recipe_summaries") or {})
    if not summaries:
        raise ValueError("fixed recipe summaries must not be empty")
    return max(
        summaries.keys(),
        key=lambda recipe_id: _economic_balance_rank(dict(summaries[recipe_id]), policy_config),
    )


__all__ = [
    "DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS",
    "DEFAULT_STAGE2_CALIBRATION_POLICY",
    "_best_recipe_id",
    "_candidate_stage2_policy",
    "_evaluate_window",
    "_is_current_stage2_policy",
    "_normalize_grid",
    "_stage2_selected_frame",
]
