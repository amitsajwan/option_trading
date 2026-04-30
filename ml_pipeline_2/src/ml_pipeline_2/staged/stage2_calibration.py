from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any, Dict, Sequence

from ..experiment_control.state import utc_now
from .pipeline import _economic_balance_rank
from .stage2_diagnostic_common import build_stage2_scored_window_frame, load_stage2_diagnostic_context
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


def _centered_grid(value: float, *, step: float = 0.05, minimum: float = 0.0, maximum: float = 1.0) -> list[float]:
    candidates = [value - step, value, value + step]
    clipped = [min(maximum, max(minimum, float(v))) for v in candidates]
    return _normalize_grid(clipped, minimum=minimum, maximum=maximum)


def _side_gap(actionable: Dict[str, Any]) -> float:
    selected_ce = ((actionable.get("selected_direction") or {}).get("ce_pct"))
    oracle_ce = ((actionable.get("oracle_direction_for_selected") or {}).get("ce_pct"))
    if selected_ce is None or oracle_ce is None:
        return 1.0
    return abs(float(selected_ce) - float(oracle_ce))


def _candidate_rank(validation_eval: Dict[str, Any], *, selected_recipe_id: str, policy_config: Dict[str, Any]) -> tuple[float, ...]:
    recipe_summary = dict((validation_eval.get("fixed_recipe_summaries") or {}).get(str(selected_recipe_id)) or {})
    actionable = dict(validation_eval.get("actionable") or {})
    agreement = float(actionable.get("selected_vs_oracle_agreement") or 0.0)
    return _economic_balance_rank(
        recipe_summary,
        policy_config,
        -_side_gap(actionable),
        agreement,
    )


def run_stage2_calibration_diagnostic(
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
        context_label="stage2 calibration",
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
        else _centered_grid(float(stage2_policy.get("selected_trade_threshold", 0.5)))
    )
    ce_grid = (
        _normalize_grid(ce_threshold_grid, minimum=0.0, maximum=1.0)
        if ce_threshold_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_ce_threshold", 0.5)))
    )
    pe_grid = (
        _normalize_grid(pe_threshold_grid, minimum=0.0, maximum=1.0)
        if pe_threshold_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_pe_threshold", 0.5)))
    )
    edge_grid = (
        _normalize_grid(min_edge_grid, minimum=0.0, maximum=1.0)
        if min_edge_grid is not None
        else _centered_grid(float(stage2_policy.get("selected_min_edge", 0.0)), minimum=0.0, maximum=1.0)
    )

    uses_trade_gate = current_policy_id in {"direction_gate_threshold_v1", "direction_gate_economic_balance_v1"}
    trade_values = trade_grid if uses_trade_gate else [None]

    rows: list[dict[str, Any]] = []
    for trade_threshold, ce_threshold, pe_threshold, min_edge in product(trade_values, ce_grid, pe_grid, edge_grid):
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
        selected_recipe_id = _best_recipe_id(validation_eval, policy_config=policy_config)
        rows.append(
            {
                "policy_id": current_policy_id,
                "trade_threshold": None if trade_threshold is None else float(trade_threshold),
                "ce_threshold": float(ce_threshold),
                "pe_threshold": float(pe_threshold),
                "min_edge": float(min_edge),
                "is_current_policy": _is_current_stage2_policy(
                    stage2_policy,
                    uses_trade_gate=uses_trade_gate,
                    trade_threshold=trade_threshold,
                    ce_threshold=ce_threshold,
                    pe_threshold=pe_threshold,
                    min_edge=min_edge,
                ),
                "validation_selected_recipe_id": str(selected_recipe_id),
                "validation": validation_eval,
                "holdout": holdout_eval,
            }
        )

    winner = max(
        rows,
        key=lambda row: _candidate_rank(
            dict(row["validation"]),
            selected_recipe_id=str(row["validation_selected_recipe_id"]),
            policy_config=policy_config,
        ),
    )

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage2_calibration_diagnostic")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "stage2_calibration_summary.json"

    payload = {
        "analysis_kind": "stage2_calibration_diagnostic_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": context.source_run_id,
        "stage1_entry_threshold": entry_threshold,
        "current_stage2_policy": dict(stage2_policy),
        "grid": {
            "policy_id": current_policy_id,
            "trade_threshold_grid": trade_grid if uses_trade_gate else [],
            "ce_threshold_grid": ce_grid,
            "pe_threshold_grid": pe_grid,
            "min_edge_grid": edge_grid,
        },
        "validation_policy": dict(policy_config),
        "fixed_recipe_ids": list(context.fixed_recipe_ids),
        "recipe_universe_recipe_ids": [str(recipe.recipe_id) for recipe in recipe_universe],
        "winner": winner,
        "rows": rows,
        "paths": {
            "analysis_root": str(analysis_root),
            "stage2_calibration_summary": str(summary_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS",
    "DEFAULT_STAGE2_CALIBRATION_POLICY",
    "run_stage2_calibration_diagnostic",
]
