from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import pandas as pd

from ..dataset_windowing import filter_trade_dates, load_feature_frame, paths_overlap
from ..evaluation import FuturesPromotionGates, evaluate_futures_stages_from_frame, positive_rate_diagnostics, stage_b
from ..experiment_control.state import RunContext, utc_now
from ..inference_contract import predict_probabilities_from_frame
from ..labeling import EffectiveLabelConfig, build_label_lineage, build_labeled_dataset, prepare_snapshot_labeled_frame
from ..model_search import run_training_cycle_catalog
from ..contracts.types import LabelRecipe, PreprocessConfig, TradingObjectiveConfig


def _side_penalty(long_share: float) -> float:
    return abs(float(long_share) - 0.5)


def _side_disqualified(long_share: float) -> bool:
    return float(long_share) < 0.10 or float(long_share) > 0.90


def _candidate_is_viable(candidate: Dict[str, Any]) -> bool:
    return bool(candidate.get("stage_a_passed")) and bool(candidate.get("positive_net_return")) and not bool(candidate.get("side_disqualified"))


def _candidate_rank_key(candidate: Dict[str, Any]) -> tuple[float, ...]:
    trades = int(candidate.get("trades", 0))
    return (float(candidate.get("profit_factor", float("-inf"))), float(int(trades >= 40)), float(trades), -float(candidate.get("side_penalty", 1e9)))


def _side_share_in_band(long_share: float, *, cfg: Dict[str, Any]) -> bool:
    return float(cfg["holdout_side_share_min"]) <= float(long_share) <= float(cfg["holdout_side_share_max"])


def _phase2_baseline_rank_key(candidate: Dict[str, Any], *, acceptance: Dict[str, Any]) -> tuple[float, ...]:
    trades = int(candidate.get("trades", 0))
    return (
        float(int(bool(candidate.get("stage_a_passed")))),
        float(int(_side_share_in_band(float(candidate.get("long_share", 0.0)), cfg=acceptance))),
        float(int(bool(candidate.get("positive_net_return")))),
        float(candidate.get("profit_factor", float("-inf"))),
        float(candidate.get("net_return_sum", float("-inf"))),
        float(trades),
        -float(candidate.get("side_penalty", 1e9)),
    )


def _assert_safe_output_root(*, out_root: Path, source_seed_dir: Path, model_window_features_path: Path, holdout_features_path: Path) -> None:
    if paths_overlap(out_root, source_seed_dir):
        raise ValueError(f"output root overlaps frozen seed directory: out_root={out_root} seed_dir={source_seed_dir}")
    if paths_overlap(out_root, model_window_features_path):
        raise ValueError(f"output root overlaps read-only model-window features: {model_window_features_path}")
    if paths_overlap(out_root, holdout_features_path):
        raise ValueError(f"output root overlaps read-only holdout features: {holdout_features_path}")


def _label_cfg(recipe: LabelRecipe) -> EffectiveLabelConfig:
    return EffectiveLabelConfig(horizon_minutes=int(recipe.horizon_minutes), return_threshold=0.0, use_excursion_gate=False, min_favorable_excursion=0.0, max_adverse_excursion=0.0, stop_loss_pct=float(recipe.stop_loss_pct), take_profit_pct=float(recipe.take_profit_pct), allow_hold_extension=False, extension_trigger_profit_pct=0.0)


def _preprocess_cfg(payload: Dict[str, Any]) -> PreprocessConfig:
    return PreprocessConfig(max_missing_rate=float(payload.get("max_missing_rate", 0.35)), clip_lower_q=float(payload.get("clip_lower_q", 0.01)), clip_upper_q=float(payload.get("clip_upper_q", 0.99)))


def _utility_cfg(payload: Dict[str, Any], *, ce_threshold: Optional[float] = None, pe_threshold: Optional[float] = None) -> TradingObjectiveConfig:
    return TradingObjectiveConfig(ce_threshold=float(payload.get("ce_threshold") if ce_threshold is None else ce_threshold), pe_threshold=float(payload.get("pe_threshold") if pe_threshold is None else pe_threshold), cost_per_trade=float(payload.get("cost_per_trade", 0.0006)), min_profit_factor=float(payload.get("min_profit_factor", 1.0)), max_equity_drawdown_pct=float(payload.get("max_equity_drawdown_pct", 0.2)), min_trades=int(payload.get("min_trades", 25)), take_profit_pct=float(payload.get("take_profit_pct", 0.0025)), stop_loss_pct=float(payload.get("stop_loss_pct", 0.0010)), discard_time_stop=bool(payload.get("discard_time_stop", False)), risk_per_trade_pct=float(payload.get("risk_per_trade_pct", 0.01)))


def _gates(payload: Dict[str, Any]) -> FuturesPromotionGates:
    merged = FuturesPromotionGates().__dict__ | dict(payload or {})
    return FuturesPromotionGates(**merged)


def _build_labeled_frame(features: pd.DataFrame, recipe: LabelRecipe, *, context: str) -> tuple[pd.DataFrame, Dict[str, Any]]:
    labeled = build_labeled_dataset(features=features.copy(), cfg=_label_cfg(recipe))
    labeled = prepare_snapshot_labeled_frame(labeled, context=context)
    return labeled, build_label_lineage(labeled, _label_cfg(recipe))


def _summarize_training_report(report: Dict[str, Any]) -> Dict[str, Any]:
    best = dict(report.get("best_experiment") or {})
    return {"objective": report.get("objective"), "label_target": report.get("label_target"), "rows_total": int(report.get("rows_total", 0)), "days_total": int(report.get("days_total", 0)), "experiments_total": int(report.get("experiments_total", 0)), "best_experiment": {"experiment_id": best.get("experiment_id"), "feature_set": best.get("feature_set"), "model_name": ((best.get("model") or {}).get("name")), "model_family": ((best.get("model") or {}).get("family")), "feature_count": best.get("feature_count"), "objective_value": best.get("objective_value"), "fallback_objective_value": best.get("fallback_objective_value"), "selected_by_fallback": bool(best.get("selected_by_fallback", False))}}


def _evaluate_candidate(train_frame: pd.DataFrame, eval_frame: pd.DataFrame, model_package: Dict[str, Any], training_report: Dict[str, Any], recipe: LabelRecipe, ce_threshold: float, pe_threshold: float, *, gates: FuturesPromotionGates, phase_name: str, eval_name: str) -> Dict[str, Any]:
    probs, input_contract = predict_probabilities_from_frame(eval_frame, model_package, missing_policy_override="error", context=f"{phase_name}:{eval_name}")
    stage_eval = evaluate_futures_stages_from_frame(frame=eval_frame, probs=probs, ce_threshold=float(ce_threshold), pe_threshold=float(pe_threshold), cost_per_trade=0.0006, gates=gates)
    raw_stage_b = stage_b(frame=eval_frame, probs=probs, ce_threshold=float(ce_threshold), pe_threshold=float(pe_threshold), cost_per_trade=0.0006, gates=gates)
    long_share = float(raw_stage_b.get("long_share", 0.0))
    summary = {
        "status": "completed",
        "created_at_utc": utc_now(),
        "phase": str(phase_name),
        "eval_name": str(eval_name),
        "recipe": recipe.to_dict(),
        "training": _summarize_training_report(training_report),
        "model_name": ((training_report.get("best_experiment") or {}).get("model") or {}).get("name"),
        "feature_set": (training_report.get("best_experiment") or {}).get("feature_set"),
        "thresholds": {"ce": float(ce_threshold), "pe": float(pe_threshold), "cost_per_trade": 0.0006},
        "input_contract": input_contract,
        "positive_rate_diagnostics": positive_rate_diagnostics(training_frame=train_frame, holdout_frame=eval_frame, gap_flag_threshold=0.08),
        "stage_eval_default": stage_eval,
        "raw_stage_b_utility": raw_stage_b,
        "stage_a_passed": bool(((stage_eval.get("stage_a_predictive_quality") or {}).get("passed"))),
        "stage_b_passed": bool(((stage_eval.get("stage_b_futures_utility") or {}).get("passed"))),
        "promotion_eligible": bool(((stage_eval.get("promotion_gates") or {}).get("promotion_eligible"))),
        "trades": int(raw_stage_b.get("trades", 0)),
        "long_trades": int(raw_stage_b.get("long_trades", 0)),
        "short_trades": int(raw_stage_b.get("short_trades", 0)),
        "long_share": long_share,
        "short_share": float(raw_stage_b.get("short_share", 0.0)),
        "side_penalty": _side_penalty(long_share),
        "side_disqualified": _side_disqualified(long_share),
        "profit_factor": float(raw_stage_b.get("profit_factor", 0.0)),
        "net_return_sum": float(raw_stage_b.get("net_return_sum", 0.0)),
        "positive_net_return": bool(float(raw_stage_b.get("net_return_sum", 0.0)) > 0.0),
        "block_rate": float(raw_stage_b.get("block_rate", 0.0)),
        "rows_total": int(raw_stage_b.get("rows_total", 0)),
    }
    summary["viable_for_selection"] = _candidate_is_viable(summary)
    return summary


def _train_single_candidate(train_frame: pd.DataFrame, *, resolved: Dict[str, Any], model_name: str, threshold: float) -> Dict[str, Any]:
    training = dict(resolved["training"])
    return run_training_cycle_catalog(
        labeled_df=train_frame,
        feature_profile=str(resolved["catalog"]["feature_profile"]),
        objective=str(training["objective"]),
        train_days=int(training["cv_config"]["train_days"]),
        valid_days=int(training["cv_config"]["valid_days"]),
        test_days=int(training["cv_config"]["test_days"]),
        step_days=int(training["cv_config"]["step_days"]),
        purge_days=int(training["cv_config"].get("purge_days", 0)),
        embargo_days=int(training["cv_config"].get("embargo_days", 0)),
        purge_mode=str(training["cv_config"].get("purge_mode", "days")),
        embargo_rows=int(training["cv_config"].get("embargo_rows", 0)),
        event_end_col=training["cv_config"].get("event_end_col"),
        random_state=42,
        max_experiments=1,
        preprocess_cfg=_preprocess_cfg(training["preprocess"]),
        label_target=str(training["label_target"]),
        utility_cfg=_utility_cfg(training["utility"], ce_threshold=threshold, pe_threshold=threshold),
        model_whitelist=[model_name],
        feature_set_whitelist=list(resolved["catalog"]["feature_sets"]),
        fit_all_final_models=False,
    )


def _run_full_window_candidate(labeled_frame: pd.DataFrame, eval_frame: pd.DataFrame, recipe: LabelRecipe, *, resolved: Dict[str, Any], model_name: str, threshold: float, out_dir: Path, gates: FuturesPromotionGates, phase_name: str) -> Dict[str, Any]:
    training_result = _train_single_candidate(labeled_frame, resolved=resolved, model_name=model_name, threshold=threshold)
    training_report = dict(training_result["report"])
    model_package = dict(training_result["model_package"])
    out_dir.mkdir(parents=True, exist_ok=True)
    training_report_path = out_dir / "training_report.json"
    model_path = out_dir / "model.joblib"
    training_report_path.write_text(json.dumps(training_report, indent=2), encoding="utf-8")
    joblib.dump(model_package, model_path)
    candidate = _evaluate_candidate(labeled_frame, eval_frame, model_package, training_report, recipe, threshold, threshold, gates=gates, phase_name=phase_name, eval_name="true_holdout")
    candidate["training_report_path"] = str(training_report_path)
    candidate["model_package_path"] = str(model_path)
    (out_dir / "candidate_summary.json").write_text(json.dumps(candidate, indent=2), encoding="utf-8")
    return candidate


def run_phase2_label_sweep(ctx: RunContext) -> Dict[str, Any]:
    resolved = ctx.resolved_config
    inputs = resolved["inputs"]
    windows = resolved["windows"]
    scenario = resolved["scenario"]
    out_root = ctx.output_root
    model_window_path = Path(inputs["model_window_features_path"])
    holdout_path = Path(inputs["holdout_features_path"])
    source_seed_dir = model_window_path.parent.resolve()
    _assert_safe_output_root(out_root=out_root, source_seed_dir=source_seed_dir, model_window_features_path=model_window_path, holdout_features_path=holdout_path)
    model_window_features = load_feature_frame(model_window_path)
    holdout_features = load_feature_frame(holdout_path)
    gates = _gates(dict(scenario.get("evaluation_gates") or {}))
    recipe_results: List[Dict[str, Any]] = []
    global_candidates: List[Dict[str, Any]] = []
    ctx.append_state("phase2_label_selection_start", recipes=len(list(scenario["recipes"])))
    for recipe_payload in list(scenario["recipes"]):
        recipe = LabelRecipe(**dict(recipe_payload))
        recipe_root = out_root / "recipes" / recipe.recipe_id
        ctx.append_state("recipe_start", recipe_id=recipe.recipe_id)
        labeled, lineage = _build_labeled_frame(model_window_features, recipe, context=f"phase2:{recipe.recipe_id}:model_window")
        model_window_labeled = filter_trade_dates(labeled, windows["full_model"]["start"], windows["full_model"]["end"])
        research_train = filter_trade_dates(model_window_labeled, windows["full_model"]["start"], windows["research_train"]["end"])
        research_valid = filter_trade_dates(model_window_labeled, windows["research_valid"]["start"], windows["research_valid"]["end"])
        recipe_root.mkdir(parents=True, exist_ok=True)
        model_window_path_out = recipe_root / "model_window_labeled.parquet"
        research_train_path = recipe_root / "research_train_labeled.parquet"
        research_valid_path = recipe_root / "research_valid_labeled.parquet"
        model_window_labeled.to_parquet(model_window_path_out, index=False)
        research_train.to_parquet(research_train_path, index=False)
        research_valid.to_parquet(research_valid_path, index=False)
        (recipe_root / "label_lineage.json").write_text(json.dumps(lineage, indent=2), encoding="utf-8")
        training_result = _train_single_candidate(research_train, resolved=resolved, model_name=str(scenario["default_model"]), threshold=float(resolved["training"]["utility"]["ce_threshold"]))
        training_report = dict(training_result["report"])
        model_package = dict(training_result["model_package"])
        model_root = recipe_root / f"step3_{scenario['default_model']}"
        model_root.mkdir(parents=True, exist_ok=True)
        training_report_path = model_root / "training_report.json"
        training_report_path.write_text(json.dumps(training_report, indent=2), encoding="utf-8")
        model_path = model_root / "model.joblib"
        joblib.dump(model_package, model_path)
        candidates: List[Dict[str, Any]] = []
        for threshold in list(scenario["threshold_grid"]):
            candidate = _evaluate_candidate(research_train, research_valid, model_package, training_report, recipe, float(threshold), float(threshold), gates=gates, phase_name="label_selection", eval_name=f"{recipe.recipe_id}_validation")
            candidate["training_report_path"] = str(training_report_path)
            candidate["model_package_path"] = str(model_path)
            candidate_dir = model_root / "candidates" / f"ce_{float(threshold):.2f}_pe_{float(threshold):.2f}"
            candidate_dir.mkdir(parents=True, exist_ok=True)
            candidate_path = candidate_dir / "candidate_summary.json"
            candidate_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
            candidate["candidate_summary_path"] = str(candidate_path)
            candidates.append(candidate)
            global_candidates.append(candidate)
        viable = [candidate for candidate in candidates if bool(candidate.get("viable_for_selection"))]
        best = max(viable, key=_candidate_rank_key) if viable else None
        recipe_summary = {"recipe": recipe.to_dict(), "model_window_labeled_path": str(model_window_path_out), "research_train_labeled_path": str(research_train_path), "research_valid_labeled_path": str(research_valid_path), "training_report_path": str(training_report_path), "model_package_path": str(model_path), "candidates": candidates, "selected_candidate": best, "status": "selected" if best is not None else "no_viable_threshold"}
        (recipe_root / "selection_summary.json").write_text(json.dumps(recipe_summary, indent=2), encoding="utf-8")
        recipe_results.append(recipe_summary)
        ctx.append_state("recipe_done", recipe_id=recipe.recipe_id, status=recipe_summary["status"], selected_threshold=(best or {}).get("thresholds"))
    all_viable = [candidate for candidate in global_candidates if bool(candidate.get("viable_for_selection"))]
    if not all_viable:
        summary = {"created_at_utc": utc_now(), "status": "no_viable_label_recipe", "step3": {"status": "no_viable_label_recipe", "recipes": recipe_results, "selected_recipe": None, "selected_candidate": None}}
        ctx.write_json("phase2_summary.json", summary)
        return summary
    selected_candidate = max(all_viable, key=_candidate_rank_key)
    selected_recipe = next(recipe for recipe in recipe_results if recipe["recipe"]["recipe_id"] == selected_candidate["recipe"]["recipe_id"])
    stress_results: List[Dict[str, Any]] = []
    ctx.append_state("phase2_model_stress_start", baseline_recipe_ids=list(scenario["baseline_recipe_ids"]))
    for recipe_id in list(scenario["baseline_recipe_ids"]):
        recipe_summary = next((recipe for recipe in recipe_results if recipe["recipe"]["recipe_id"] == recipe_id), None)
        if recipe_summary is None or recipe_summary.get("selected_candidate") is None:
            continue
        recipe = LabelRecipe(**dict(recipe_summary["recipe"]))
        threshold = float(recipe_summary["selected_candidate"]["thresholds"]["ce"])
        train_frame = pd.read_parquet(recipe_summary["research_train_labeled_path"])
        valid_frame = pd.read_parquet(recipe_summary["research_valid_labeled_path"])
        stress_root = out_root / "model_stress" / recipe.recipe_id
        model_candidates: List[Dict[str, Any]] = []
        for model_name in list(scenario["stress_models"]):
            training_result = _train_single_candidate(train_frame, resolved=resolved, model_name=str(model_name), threshold=threshold)
            training_report = dict(training_result["report"])
            model_package = dict(training_result["model_package"])
            model_root = stress_root / str(model_name)
            model_root.mkdir(parents=True, exist_ok=True)
            training_report_path = model_root / "training_report.json"
            training_report_path.write_text(json.dumps(training_report, indent=2), encoding="utf-8")
            model_path = model_root / "model.joblib"
            joblib.dump(model_package, model_path)
            candidate = _evaluate_candidate(train_frame, valid_frame, model_package, training_report, recipe, threshold, threshold, gates=gates, phase_name="model_stress", eval_name=f"{recipe.recipe_id}_validation")
            candidate["training_report_path"] = str(training_report_path)
            candidate["model_package_path"] = str(model_path)
            candidate["candidate_summary_path"] = str(model_root / "candidate_summary.json")
            Path(candidate["candidate_summary_path"]).write_text(json.dumps(candidate, indent=2), encoding="utf-8")
            model_candidates.append(candidate)
        viable = [candidate for candidate in model_candidates if bool(candidate.get("viable_for_selection"))]
        stress_summary = {"status": "ok" if viable else "no_viable_model_for_label", "selected_recipe": recipe_summary, "selected_threshold": recipe_summary["selected_candidate"]["thresholds"], "stress_results": model_candidates, "selected_model_candidate": max(viable, key=_candidate_rank_key) if viable else None}
        stress_root.mkdir(parents=True, exist_ok=True)
        (stress_root / "model_stress_summary.json").write_text(json.dumps(stress_summary, indent=2), encoding="utf-8")
        stress_results.append(stress_summary)
    selected_stress = next((row for row in stress_results if row.get("selected_model_candidate") is not None), None)
    if selected_stress is None:
        summary = {"created_at_utc": utc_now(), "status": "no_viable_model_for_label", "step3": {"selected_recipe": selected_recipe, "selected_candidate": selected_candidate}, "step4": {"stress_results": stress_results, "selected_stress_result": None}}
        ctx.write_json("phase2_summary.json", summary)
        return summary
    holdout_results: List[Dict[str, Any]] = []
    acceptance = dict(scenario.get("acceptance") or {"holdout_side_share_min": 0.35, "holdout_side_share_max": 0.65})
    for stress_summary in stress_results:
        if stress_summary.get("selected_model_candidate") is None:
            continue
        recipe = LabelRecipe(**dict(stress_summary["selected_recipe"]["recipe"]))
        threshold = float(stress_summary["selected_model_candidate"]["thresholds"]["ce"])
        model_name = str(stress_summary["selected_model_candidate"]["model_name"] or scenario["default_model"])
        recipe_root = out_root / "final_holdout" / recipe.recipe_id
        holdout_labeled, lineage = _build_labeled_frame(holdout_features, recipe, context=f"phase2:{recipe.recipe_id}:holdout")
        holdout_labeled = filter_trade_dates(holdout_labeled, windows["final_holdout"]["start"], windows["final_holdout"]["end"])
        holdout_labeled_path = recipe_root / "holdout_labeled.parquet"
        recipe_root.mkdir(parents=True, exist_ok=True)
        holdout_labeled.to_parquet(holdout_labeled_path, index=False)
        (recipe_root / "holdout_label_lineage.json").write_text(json.dumps(lineage, indent=2), encoding="utf-8")
        full_model_window_labeled = pd.read_parquet(stress_summary["selected_recipe"]["model_window_labeled_path"])
        full_model_window_labeled = filter_trade_dates(full_model_window_labeled, windows["full_model"]["start"], windows["full_model"]["end"])
        candidate = _run_full_window_candidate(full_model_window_labeled, holdout_labeled, recipe, resolved=resolved, model_name=model_name, threshold=threshold, out_dir=recipe_root / "trained", gates=gates, phase_name=f"final_holdout_{recipe.recipe_id}")
        summary_row = {"recipe": stress_summary["selected_recipe"]["recipe"], "selected_threshold": float(threshold), "selected_model_name": model_name, "candidate": candidate, "side_share_in_band": _side_share_in_band(float(candidate.get("long_share", 0.0)), cfg=acceptance), "paths": {"model_window_labeled": str(stress_summary["selected_recipe"]["model_window_labeled_path"]), "holdout_labeled": str(holdout_labeled_path)}}
        (recipe_root / "holdout_summary.json").write_text(json.dumps(summary_row, indent=2), encoding="utf-8")
        holdout_results.append(summary_row)
    if not holdout_results:
        summary = {"created_at_utc": utc_now(), "status": "no_holdout_candidates", "step3": {"selected_recipe": selected_recipe, "selected_candidate": selected_candidate}, "step4": {"stress_results": stress_results, "selected_stress_result": selected_stress}}
        ctx.write_json("phase2_summary.json", summary)
        return summary
    winner = max(holdout_results, key=lambda row: _phase2_baseline_rank_key(dict(row.get("candidate") or {}), acceptance=acceptance))
    baseline = {"created_at_utc": utc_now(), "status": "completed", "holdout_window": dict(windows["final_holdout"]), "decision_config": {"cost_per_trade": 0.0006, "ce_threshold": float(winner["selected_threshold"]), "pe_threshold": float(winner["selected_threshold"])}, "acceptance_rules": {"stage_a_must_pass": True, "side_share_range": [acceptance["holdout_side_share_min"], acceptance["holdout_side_share_max"]], "profit_factor_regression_tolerance": 0.10}, "candidates": holdout_results, "winner": winner}
    baseline_path = ctx.write_json("phase2_binary_baseline.json", baseline)
    summary = {"created_at_utc": utc_now(), "status": "completed", "step3": {"selected_recipe": selected_recipe, "selected_candidate": selected_candidate}, "step4": {"stress_results": stress_results, "selected_stress_result": selected_stress}, "phase2_binary_baseline": baseline, "phase2_binary_baseline_path": str(baseline_path)}
    ctx.write_json("phase2_summary.json", summary)
    return summary
