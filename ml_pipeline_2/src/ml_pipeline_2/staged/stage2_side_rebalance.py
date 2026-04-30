from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import numpy as np
import pandas as pd

from ..experiment_control.state import utc_now
from .pipeline import _economic_balance_rank
from .skew_diagnostic import _dir_counts, _oracle_level_summary
from .stage2_policy_core import (
    DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS,
    DEFAULT_STAGE2_CALIBRATION_POLICY,
    _best_recipe_id,
    _candidate_stage2_policy,
    _evaluate_window,
    _is_current_stage2_policy,
    _normalize_grid,
    _stage2_selected_frame,
)
from .stage2_diagnostic_common import build_stage2_scored_window_frame, load_stage2_diagnostic_context


def _expanded_grid(
    value: float,
    *,
    lower_steps: int,
    upper_steps: int,
    step: float = 0.05,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> list[float]:
    values = [float(value) + (step * float(offset)) for offset in range(-int(lower_steps), int(upper_steps) + 1)]
    clipped = [min(maximum, max(minimum, v)) for v in values]
    return _normalize_grid(clipped, minimum=minimum, maximum=maximum)


def _stage1_positive_oracle_frame(merged: pd.DataFrame, *, entry_threshold: float) -> pd.DataFrame:
    if len(merged) == 0:
        return merged.copy()
    entry_prob = pd.to_numeric(
        merged["entry_prob"] if "entry_prob" in merged.columns else pd.Series([0.0] * len(merged), index=merged.index),
        errors="coerce",
    ).fillna(0.0)
    stage1_positive = merged.loc[entry_prob >= float(entry_threshold)].copy()
    if "entry_label" not in stage1_positive.columns:
        return stage1_positive.iloc[0:0].copy()
    oracle_positive = pd.to_numeric(stage1_positive["entry_label"], errors="coerce").fillna(0).astype(int) == 1
    return stage1_positive.loc[oracle_positive].copy().reset_index(drop=True)


def _safe_ratio(numer: int, denom: int) -> float | None:
    if int(denom) <= 0:
        return None
    return round(float(numer) / float(denom), 4)


def _side_precision(selected: pd.DataFrame, *, selected_side: str, oracle_side: str) -> float | None:
    if len(selected) == 0 or "selected_side" not in selected.columns or "direction_label" not in selected.columns:
        return None
    subset = selected.loc[selected["selected_side"].astype(str).eq(str(selected_side))]
    if len(subset) == 0:
        return None
    return round(float(subset["direction_label"].astype(str).eq(str(oracle_side)).mean()), 4)


def _stage2_capture_summary(selected: pd.DataFrame, baseline: pd.DataFrame) -> Dict[str, Any]:
    baseline_counts = _dir_counts(baseline, "direction_label")
    selected_counts = _dir_counts(selected, "selected_side")
    oracle_on_selected = _dir_counts(selected, "direction_label")
    baseline_ce = int(baseline_counts.get("ce") or 0)
    baseline_pe = int(baseline_counts.get("pe") or 0)
    selected_oracle_ce = int(oracle_on_selected.get("ce") or 0)
    selected_oracle_pe = int(oracle_on_selected.get("pe") or 0)
    selected_total = int(len(selected))
    oracle_positive_selected = selected_oracle_ce + selected_oracle_pe
    selected_ce_pct = selected_counts.get("ce_pct")
    baseline_ce_pct = baseline_counts.get("ce_pct")
    selected_gap = None
    if selected_ce_pct is not None and baseline_ce_pct is not None:
        selected_gap = round(abs(float(selected_ce_pct) - float(baseline_ce_pct)), 4)
    return {
        "stage1_oracle_direction": baseline_counts,
        "selected_direction": selected_counts,
        "oracle_direction_for_selected": oracle_on_selected,
        "stage1_oracle_positive_count": int(len(baseline)),
        "selected_total": selected_total,
        "selected_oracle_positive_count": int(oracle_positive_selected),
        "selected_other_count": int(max(selected_total - oracle_positive_selected, 0)),
        "oracle_positive_rate": _safe_ratio(oracle_positive_selected, selected_total),
        "ce_capture_vs_stage1_oracle": _safe_ratio(selected_oracle_ce, baseline_ce),
        "pe_capture_vs_stage1_oracle": _safe_ratio(selected_oracle_pe, baseline_pe),
        "selected_side_gap_to_stage1_oracle": selected_gap,
        "selected_vs_oracle_agreement": (
            round(float((selected["selected_side"].astype(str) == selected["direction_label"].astype(str)).mean()), 4)
            if len(selected) and "selected_side" in selected.columns and "direction_label" in selected.columns
            else None
        ),
        "selected_ce_precision": _side_precision(selected, selected_side="CE", oracle_side="CE"),
        "selected_pe_precision": _side_precision(selected, selected_side="PE", oracle_side="PE"),
    }


def _ensure_selected_schema(selected: pd.DataFrame, *, recipe_universe: Sequence[Any]) -> pd.DataFrame:
    out = selected.copy()
    if "selected_side" not in out.columns:
        out["selected_side"] = pd.Series(dtype=object, index=out.index)
    if "oracle_selected_side_return" not in out.columns:
        out["oracle_selected_side_return"] = pd.Series(dtype=float, index=out.index)
    for recipe in recipe_universe:
        recipe_id = str(recipe.recipe_id)
        selected_col = f"{recipe_id}__selected_return"
        if selected_col not in out.columns:
            out[selected_col] = pd.Series(dtype=float, index=out.index)
    return out


def _mean_precision(capture: Dict[str, Any]) -> float:
    ce_precision = capture.get("selected_ce_precision")
    pe_precision = capture.get("selected_pe_precision")
    values = [float(v) for v in (ce_precision, pe_precision) if v is not None]
    return float(sum(values) / len(values)) if values else 0.0


def _capture_gap(capture: Dict[str, Any]) -> float:
    gap = capture.get("selected_side_gap_to_stage1_oracle")
    return float(gap) if gap is not None else 1.0


def _alignment_rank(row: Dict[str, Any], *, window_key: str) -> tuple[float, ...]:
    window_eval = dict(row.get(window_key) or {})
    capture = dict(window_eval.get("capture") or {})
    recipe_id = str(row.get(f"{window_key}_selected_recipe_id") or "")
    recipe_summary = dict((window_eval.get("fixed_recipe_summaries") or {}).get(recipe_id) or {})
    return (
        -_capture_gap(capture),
        float(capture.get("selected_vs_oracle_agreement") or 0.0),
        _mean_precision(capture),
        float(capture.get("oracle_positive_rate") or 0.0),
        float(recipe_summary.get("net_return_sum") or float("-inf")),
        float(recipe_summary.get("profit_factor") or float("-inf")),
        float(recipe_summary.get("trades") or 0.0),
    )


def _compromise_rank(row: Dict[str, Any], *, policy_config: Dict[str, Any]) -> tuple[float, ...]:
    validation_eval = dict(row.get("validation") or {})
    capture = dict(validation_eval.get("capture") or {})
    recipe_id = str(row.get("validation_selected_recipe_id") or "")
    recipe_summary = dict((validation_eval.get("fixed_recipe_summaries") or {}).get(recipe_id) or {})
    ce_capture = float(capture.get("ce_capture_vs_stage1_oracle") or 0.0)
    pe_capture = float(capture.get("pe_capture_vs_stage1_oracle") or 0.0)
    return _economic_balance_rank(
        recipe_summary,
        policy_config,
        -_capture_gap(capture),
        float(capture.get("selected_vs_oracle_agreement") or 0.0),
        _mean_precision(capture),
        float(capture.get("oracle_positive_rate") or 0.0),
        -abs(ce_capture - pe_capture),
    )


def run_stage2_side_rebalance_diagnostic(
    *,
    run_dir: str | Path,
    fixed_recipe_ids: Sequence[str] = DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS,
    trade_threshold_grid: Sequence[float] | None = None,
    ce_threshold_grid: Sequence[float] | None = None,
    pe_threshold_grid: Sequence[float] | None = None,
    min_edge_grid: Sequence[float] | None = None,
    validation_policy: Dict[str, Any] | None = None,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    policy_config = {**DEFAULT_STAGE2_CALIBRATION_POLICY, **dict(validation_policy or {})}
    context = load_stage2_diagnostic_context(
        run_dir=run_dir,
        fixed_recipe_ids=fixed_recipe_ids,
        context_label="stage2 side rebalance",
    )
    source_run_dir = context.source_run_dir
    recipe_universe = context.recipe_universe
    diagnostic_valid = context.diagnostic_windows["research_valid"]
    diagnostic_holdout = context.diagnostic_windows["final_holdout"]
    merged_valid = build_stage2_scored_window_frame(context, window_name="research_valid")
    merged_holdout = build_stage2_scored_window_frame(context, window_name="final_holdout")

    stage1_policy = context.stage1_policy
    stage2_policy = context.stage2_policy
    current_policy_id = str(stage2_policy.get("policy_id") or "direction_gate_threshold_v1")
    entry_threshold = float(stage1_policy["selected_threshold"])

    trade_grid = (
        _normalize_grid(trade_threshold_grid, minimum=0.0, maximum=1.0)
        if trade_threshold_grid is not None
        else _expanded_grid(float(stage2_policy.get("selected_trade_threshold", 0.5)), lower_steps=1, upper_steps=1)
    )
    ce_grid = (
        _normalize_grid(ce_threshold_grid, minimum=0.0, maximum=1.0)
        if ce_threshold_grid is not None
        else _expanded_grid(float(stage2_policy.get("selected_ce_threshold", 0.5)), lower_steps=3, upper_steps=1)
    )
    pe_grid = (
        _normalize_grid(pe_threshold_grid, minimum=0.0, maximum=1.0)
        if pe_threshold_grid is not None
        else _expanded_grid(float(stage2_policy.get("selected_pe_threshold", 0.5)), lower_steps=1, upper_steps=3)
    )
    edge_grid = (
        _normalize_grid(min_edge_grid, minimum=0.0, maximum=1.0)
        if min_edge_grid is not None
        else _expanded_grid(float(stage2_policy.get("selected_min_edge", 0.0)), lower_steps=1, upper_steps=1)
    )

    valid_stage1_oracle = _stage1_positive_oracle_frame(merged_valid, entry_threshold=entry_threshold)
    holdout_stage1_oracle = _stage1_positive_oracle_frame(merged_holdout, entry_threshold=entry_threshold)
    baseline = {
        "research_valid": _oracle_level_summary(valid_stage1_oracle, int(len(diagnostic_valid))),
        "final_holdout": _oracle_level_summary(holdout_stage1_oracle, int(len(diagnostic_holdout))),
    }

    rows: list[dict[str, Any]] = []
    for trade_threshold, ce_threshold, pe_threshold, min_edge in product(trade_grid, ce_grid, pe_grid, edge_grid):
        candidate_policy = _candidate_stage2_policy(
            current_policy_id,
            trade_threshold=trade_threshold,
            ce_threshold=ce_threshold,
            pe_threshold=pe_threshold,
            min_edge=min_edge,
        )
        selected_valid = _stage2_selected_frame(
            merged_valid,
            entry_threshold=entry_threshold,
            stage2_policy=candidate_policy,
            recipe_universe=recipe_universe,
        )
        selected_holdout = _stage2_selected_frame(
            merged_holdout,
            entry_threshold=entry_threshold,
            stage2_policy=candidate_policy,
            recipe_universe=recipe_universe,
        )
        selected_valid = _ensure_selected_schema(selected_valid, recipe_universe=recipe_universe)
        selected_holdout = _ensure_selected_schema(selected_holdout, recipe_universe=recipe_universe)
        validation_eval = _evaluate_window(
            selected_valid,
            rows_total=int(len(diagnostic_valid)),
            fixed_recipe_ids=context.fixed_recipe_ids,
            policy_config=policy_config,
        )
        holdout_eval = _evaluate_window(
            selected_holdout,
            rows_total=int(len(diagnostic_holdout)),
            fixed_recipe_ids=context.fixed_recipe_ids,
            policy_config=policy_config,
        )
        validation_capture = _stage2_capture_summary(selected_valid, valid_stage1_oracle)
        holdout_capture = _stage2_capture_summary(selected_holdout, holdout_stage1_oracle)
        validation_eval["capture"] = validation_capture
        holdout_eval["capture"] = holdout_capture
        validation_recipe_id = _best_recipe_id(validation_eval, policy_config=policy_config)
        holdout_recipe_id = _best_recipe_id(holdout_eval, policy_config=policy_config)
        rows.append(
            {
                "policy_id": current_policy_id,
                "trade_threshold": float(trade_threshold),
                "ce_threshold": float(ce_threshold),
                "pe_threshold": float(pe_threshold),
                "min_edge": float(min_edge),
                "threshold_gap_pe_minus_ce": round(float(pe_threshold) - float(ce_threshold), 4),
                "is_current_policy": _is_current_stage2_policy(
                    stage2_policy,
                    uses_trade_gate=True,
                    trade_threshold=trade_threshold,
                    ce_threshold=ce_threshold,
                    pe_threshold=pe_threshold,
                    min_edge=min_edge,
                ),
                "validation_selected_recipe_id": str(validation_recipe_id),
                "holdout_selected_recipe_id": str(holdout_recipe_id),
                "validation": validation_eval,
                "holdout": holdout_eval,
            }
        )

    current_row = next((row for row in rows if bool(row.get("is_current_policy"))), None)
    validation_compromise_winner = max(rows, key=lambda row: _compromise_rank(row, policy_config=policy_config))
    validation_alignment_winner = max(rows, key=lambda row: _alignment_rank(row, window_key="validation"))
    holdout_alignment_reference = max(rows, key=lambda row: _alignment_rank(row, window_key="holdout"))

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage2_side_rebalance_diagnostic")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "stage2_side_rebalance_summary.json"

    payload = {
        "analysis_kind": "stage2_side_rebalance_diagnostic_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": context.source_run_id,
        "stage1_entry_threshold": entry_threshold,
        "current_stage2_policy": dict(stage2_policy),
        "grid": {
            "policy_id": current_policy_id,
            "trade_threshold_grid": trade_grid,
            "ce_threshold_grid": ce_grid,
            "pe_threshold_grid": pe_grid,
            "min_edge_grid": edge_grid,
        },
        "validation_policy": dict(policy_config),
        "fixed_recipe_ids": list(context.fixed_recipe_ids),
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "stage1_oracle_baseline": baseline,
        "current_policy_row": current_row,
        "winners": {
            "validation_compromise": validation_compromise_winner,
            "validation_alignment": validation_alignment_winner,
            "holdout_alignment_reference": holdout_alignment_reference,
        },
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "stage2_side_rebalance_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "run_stage2_side_rebalance_diagnostic",
]
