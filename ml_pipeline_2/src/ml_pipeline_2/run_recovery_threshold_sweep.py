from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .contracts.manifests import RECOVERY_KIND
from .contracts.types import RecoveryRecipe
from .dataset_windowing import filter_trade_dates, load_feature_frame
from .evaluation import evaluate_futures_stages_from_frame, stage_b
from .experiment_control.state import utc_now
from .inference_contract import load_model_package, predict_probabilities_from_frame
from .scenario_flows.fo_expiry_aware_recovery import (
    apply_candidate_filter,
    _effective_label_cfg,
    _gates,
    _normalize_candidate_filter,
    _prepare_labeled_frame,
    _side_penalty,
    _side_share_in_band,
    _utility_cfg,
)


DEFAULT_THRESHOLD_GRID: tuple[float, ...] = (0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _parse_threshold_grid(values: Optional[Sequence[str]]) -> List[float]:
    if not values:
        return [float(x) for x in DEFAULT_THRESHOLD_GRID]
    out: List[float] = []
    for value in values:
        for token in str(value).split(","):
            token = str(token).strip()
            if not token:
                continue
            threshold = float(token)
            if threshold < 0.0 or threshold > 1.0:
                raise ValueError(f"threshold out of range [0,1]: {threshold}")
            out.append(float(threshold))
    deduped = sorted({round(float(value), 10) for value in out})
    if not deduped:
        raise ValueError("threshold grid must not be empty")
    return [float(x) for x in deduped]


def _load_recipe_payload(*, run_dir: Path, resolved: Dict[str, Any], summary: Optional[Dict[str, Any]], recipe_id: str) -> Dict[str, Any]:
    recipe_root = run_dir / "primary_recipes" / recipe_id
    recipe_summary_path = recipe_root / "summary.json"
    if recipe_summary_path.exists():
        recipe_summary = _read_json(recipe_summary_path)
        payload = dict(recipe_summary.get("recipe") or {})
        if payload:
            return payload
    for row in list((summary or {}).get("primary_recipes") or []):
        payload = dict(row or {})
        recipe_payload = dict(payload.get("recipe") or {})
        if str(recipe_payload.get("recipe_id") or "").strip() == recipe_id:
            return recipe_payload
    for payload in list((resolved.get("scenario") or {}).get("recipes") or []):
        candidate = dict(payload or {})
        if str(candidate.get("recipe_id") or "").strip() == recipe_id:
            return candidate
    raise FileNotFoundError(f"recipe payload not found for recipe_id={recipe_id}: {run_dir}")


def _select_recipe_id(*, run_dir: Path, summary: Optional[Dict[str, Any]], recipe_id: Optional[str]) -> str:
    resolved = str(recipe_id or "").strip()
    if resolved:
        return resolved
    selected = str((summary or {}).get("selected_primary_recipe_id") or "").strip()
    if selected:
        return selected
    raise ValueError(f"recipe_id is required when run summary does not expose selected_primary_recipe_id: {run_dir}")


def _stage_row(*, holdout_labeled: pd.DataFrame, probs: pd.DataFrame, threshold: float, cost_per_trade: float, gates) -> Dict[str, Any]:
    stage_eval = evaluate_futures_stages_from_frame(
        frame=holdout_labeled,
        probs=probs,
        ce_threshold=float(threshold),
        pe_threshold=float(threshold),
        cost_per_trade=float(cost_per_trade),
        gates=gates,
    )
    raw_stage_b = stage_b(
        frame=holdout_labeled,
        probs=probs,
        ce_threshold=float(threshold),
        pe_threshold=float(threshold),
        cost_per_trade=float(cost_per_trade),
        gates=gates,
    )
    stage_a_report = dict(stage_eval.get("stage_a_predictive_quality") or {})
    long_report = dict((stage_a_report.get("sides") or {}).get("long") or {})
    short_report = dict((stage_a_report.get("sides") or {}).get("short") or {})
    long_share = float(raw_stage_b.get("long_share", 0.0))
    row = {
        "threshold": float(threshold),
        "stage_a_passed": bool(stage_a_report.get("passed")),
        "stage_b_passed": bool((stage_eval.get("stage_b_futures_utility") or {}).get("passed")),
        "promotion_eligible": bool((stage_eval.get("promotion_gates") or {}).get("promotion_eligible")),
        "trades": int(raw_stage_b.get("trades", 0)),
        "long_trades": int(raw_stage_b.get("long_trades", 0)),
        "short_trades": int(raw_stage_b.get("short_trades", 0)),
        "hold_count": int(raw_stage_b.get("hold_count", 0)),
        "rows_total": int(raw_stage_b.get("rows_total", 0)),
        "long_share": long_share,
        "short_share": float(raw_stage_b.get("short_share", 0.0)),
        "side_share_in_band": bool(_side_share_in_band(long_share)),
        "side_penalty": float(_side_penalty(long_share)),
        "block_rate": float(raw_stage_b.get("block_rate", 0.0)),
        "profit_factor": float(raw_stage_b.get("profit_factor", 0.0)),
        "gross_profit_factor": float(raw_stage_b.get("gross_profit_factor", 0.0)),
        "net_return_sum": float(raw_stage_b.get("net_return_sum", 0.0)),
        "gross_return_sum": float(raw_stage_b.get("gross_return_sum", 0.0)),
        "mean_net_return_per_trade": float(raw_stage_b.get("mean_net_return_per_trade", 0.0)),
        "mean_gross_return_per_trade": float(raw_stage_b.get("mean_gross_return_per_trade", 0.0)),
        "win_rate": float(raw_stage_b.get("win_rate", 0.0)),
        "gross_win_rate": float(raw_stage_b.get("gross_win_rate", 0.0)),
        "max_drawdown_pct": float(raw_stage_b.get("max_drawdown_pct", 0.0)),
        "tp_trades": int(raw_stage_b.get("tp_trades", 0)),
        "sl_trades": int(raw_stage_b.get("sl_trades", 0)),
        "time_stop_trades": int(raw_stage_b.get("time_stop_trades", 0)),
        "invalid_trades": int(raw_stage_b.get("invalid_trades", 0)),
        "time_stop_gross_wins": int(raw_stage_b.get("time_stop_gross_wins", 0)),
        "time_stop_gross_losses": int(raw_stage_b.get("time_stop_gross_losses", 0)),
        "time_stop_net_wins": int(raw_stage_b.get("time_stop_net_wins", 0)),
        "time_stop_net_losses": int(raw_stage_b.get("time_stop_net_losses", 0)),
        "long_roc_auc": long_report.get("roc_auc"),
        "short_roc_auc": short_report.get("roc_auc"),
        "long_brier": long_report.get("brier"),
        "short_brier": short_report.get("brier"),
        "long_roc_auc_drift_half_split": long_report.get("roc_auc_drift_half_split"),
        "short_roc_auc_drift_half_split": short_report.get("roc_auc_drift_half_split"),
    }
    return row


def _rank_row(row: Dict[str, Any]) -> tuple[float, float, float, float, int]:
    return (
        float(bool(row.get("stage_a_passed"))),
        float(bool(row.get("side_share_in_band"))),
        float(row.get("profit_factor", float("-inf"))),
        float(row.get("net_return_sum", float("-inf"))),
        int(row.get("trades", 0)),
    )


def _resolve_output_dir(*, run_dir: Path, recipe_id: str, output_dir: Optional[Path]) -> Path:
    if output_dir is not None:
        return output_dir
    return run_dir / "primary_recipes" / recipe_id / "threshold_sweep"


def sweep_recovery_thresholds(
    *,
    run_dir: Path,
    recipe_id: Optional[str] = None,
    threshold_grid: Optional[Sequence[float]] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    resolved_path = run_dir / "resolved_config.json"
    if not resolved_path.exists():
        raise FileNotFoundError(f"resolved_config.json not found: {resolved_path}")
    resolved = _read_json(resolved_path)
    if str(resolved.get("experiment_kind") or "").strip() != RECOVERY_KIND:
        raise ValueError(f"run is not a recovery experiment: {run_dir}")
    summary_path = run_dir / "summary.json"
    summary = _read_json(summary_path) if summary_path.exists() else None
    chosen_recipe_id = _select_recipe_id(run_dir=run_dir, summary=summary, recipe_id=recipe_id)
    recipe_root = run_dir / "primary_recipes" / chosen_recipe_id
    recipe_summary_path = recipe_root / "summary.json"
    model_path = recipe_root / "model.joblib"
    training_report_path = recipe_root / "training_report.json"
    if not model_path.exists():
        raise FileNotFoundError(f"model.joblib not found for recipe_id={chosen_recipe_id}: {model_path}")
    if not training_report_path.exists():
        raise FileNotFoundError(f"training_report.json not found for recipe_id={chosen_recipe_id}: {training_report_path}")
    recipe_payload = _load_recipe_payload(run_dir=run_dir, resolved=resolved, summary=summary, recipe_id=chosen_recipe_id)
    recipe = RecoveryRecipe(**recipe_payload)
    inputs = dict(resolved.get("inputs") or {})
    windows = dict(resolved.get("windows") or {})
    scenario = dict(resolved.get("scenario") or {})
    training = dict(resolved.get("training") or {})
    threshold_values = [float(x) for x in (threshold_grid or DEFAULT_THRESHOLD_GRID)]
    threshold_values = sorted({round(float(value), 10) for value in threshold_values})
    if not threshold_values:
        raise ValueError("threshold grid must not be empty")
    model_window_features = filter_trade_dates(
        load_feature_frame(Path(inputs["model_window_features_path"])),
        windows["full_model"]["start"],
        windows["full_model"]["end"],
    )
    holdout_features = filter_trade_dates(
        load_feature_frame(Path(inputs["holdout_features_path"])),
        windows["final_holdout"]["start"],
        windows["final_holdout"]["end"],
    )
    label_cfg = _effective_label_cfg(
        recipe,
        train_features=model_window_features,
        event_sampling_mode=str(scenario.get("event_sampling_mode", "none")),
        event_signal_col=scenario.get("event_signal_col"),
    )
    holdout_labeled, holdout_sampling_meta, holdout_lineage = _prepare_labeled_frame(
        holdout_features,
        recipe=recipe,
        label_cfg=label_cfg,
        event_sampling_mode=str(scenario.get("event_sampling_mode", "none")),
        context=f"recovery.threshold_sweep:{recipe.recipe_id}:holdout",
    )
    holdout_labeled, holdout_filtering_meta = apply_candidate_filter(
        holdout_labeled,
        candidate_filter=_normalize_candidate_filter(dict(scenario.get("candidate_filter") or {})),
        context=f"recovery.threshold_sweep:{recipe.recipe_id}:holdout",
    )
    model_package = load_model_package(model_path)
    probs, input_contract = predict_probabilities_from_frame(
        holdout_labeled,
        model_package,
        missing_policy_override="error",
        context=f"recovery.threshold_sweep:{recipe.recipe_id}",
    )
    if "ce_prob" not in probs.columns or "pe_prob" not in probs.columns:
        raise ValueError(f"model package is not a dual-side recovery package: {model_path}")
    gates = _gates(dict(scenario.get("evaluation_gates") or {}))
    utility_cfg = _utility_cfg(dict(training.get("utility") or {}))
    rows = [
        _stage_row(
            holdout_labeled=holdout_labeled,
            probs=probs,
            threshold=float(threshold),
            cost_per_trade=float(utility_cfg.cost_per_trade),
            gates=gates,
        )
        for threshold in threshold_values
    ]
    best = max(rows, key=_rank_row) if rows else None
    current_threshold = float(scenario.get("primary_threshold", utility_cfg.ce_threshold))
    current_row = next((row for row in rows if abs(float(row["threshold"]) - current_threshold) <= 1e-9), None)
    sweep_root = _resolve_output_dir(run_dir=run_dir, recipe_id=chosen_recipe_id, output_dir=output_dir)
    sweep_root.mkdir(parents=True, exist_ok=True)
    holdout_labeled_path = sweep_root / "holdout_labeled.parquet"
    holdout_probabilities_path = sweep_root / "holdout_probabilities.parquet"
    report_csv_path = sweep_root / "report.csv"
    summary_out_path = sweep_root / "summary.json"
    holdout_labeled.to_parquet(holdout_labeled_path, index=False)
    probs.to_parquet(holdout_probabilities_path, index=False)
    pd.DataFrame(rows).to_csv(report_csv_path, index=False)
    payload = {
        "created_at_utc": utc_now(),
        "status": "completed",
        "run_dir": str(run_dir.resolve()),
        "recipe_id": chosen_recipe_id,
        "recipe": recipe.to_dict(),
        "paths": {
            "resolved_config": str(resolved_path.resolve()),
            "run_summary": str(summary_path.resolve()) if summary_path.exists() else None,
            "recipe_summary": str(recipe_summary_path.resolve()) if recipe_summary_path.exists() else None,
            "model_package": str(model_path.resolve()),
            "training_report": str(training_report_path.resolve()),
            "holdout_labeled": str(holdout_labeled_path.resolve()),
            "holdout_probabilities": str(holdout_probabilities_path.resolve()),
            "report_csv": str(report_csv_path.resolve()),
        },
        "source_inputs": {
            "model_window_features": str(Path(inputs["model_window_features_path"]).resolve()),
            "holdout_features": str(Path(inputs["holdout_features_path"]).resolve()),
        },
        "primary_threshold": current_threshold,
        "threshold_grid": [float(x) for x in threshold_values],
        "current_threshold_row": current_row,
        "recommended_threshold": (float(best["threshold"]) if best is not None else None),
        "recommended_row": best,
        "rows": rows,
        "holdout_rows": int(len(holdout_labeled)),
        "holdout_sampling_meta": holdout_sampling_meta,
        "holdout_filtering_meta": holdout_filtering_meta,
        "holdout_label_lineage": holdout_lineage,
        "input_contract": input_contract,
    }
    _write_json(summary_out_path, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sweep primary trade thresholds for a completed recovery recipe.")
    parser.add_argument("--run-dir", required=True, help="Path to a recovery run directory")
    parser.add_argument("--recipe-id", help="Recipe id under primary_recipes/. Required if the run summary is not finished yet.")
    parser.add_argument("--threshold-grid", nargs="*", help="Threshold grid values. Supports either repeated values or comma-separated tokens.")
    parser.add_argument("--output-dir", help="Optional override output directory for threshold sweep artifacts")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = sweep_recovery_thresholds(
        run_dir=Path(args.run_dir),
        recipe_id=(str(args.recipe_id).strip() if args.recipe_id else None),
        threshold_grid=_parse_threshold_grid(args.threshold_grid),
        output_dir=(Path(args.output_dir) if args.output_dir else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
