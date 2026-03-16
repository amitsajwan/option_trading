from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import run_research
from ml_pipeline_2.labeling import EffectiveLabelConfig, build_labeled_dataset, prepare_snapshot_labeled_frame
from ml_pipeline_2.model_search import run_training_cycle_catalog
from ml_pipeline_2.contracts.types import PreprocessConfig, TradingObjectiveConfig
from ml_pipeline_2.scenario_flows.fo_expiry_aware_recovery import apply_candidate_filter
from ml_pipeline_2.tests.helpers import build_phase2_smoke_manifest, build_recovery_smoke_manifest, build_synthetic_feature_frames


def test_phase2_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_phase2_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "phase2_summary.json").exists()
    assert (output_root / "phase2_binary_baseline.json").exists()
    assert (output_root / "recipes" / "L1" / "selection_summary.json").exists()
    assert (output_root / "model_stress" / "L1" / "model_stress_summary.json").exists()


def test_recovery_smoke_runs_end_to_end(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    output_root = Path(summary["output_root"])
    assert (output_root / "resolved_config.json").exists()
    assert (output_root / "manifest_hash.txt").exists()
    assert (output_root / "state.jsonl").exists()
    assert (output_root / "summary.json").exists()
    assert (output_root / "primary_recipes" / "TB_BASE_L1" / "summary.json").exists()
    assert (output_root / "meta_gate" / "summary.json").exists()


def test_recovery_candidate_filter_helper_applies_expected_rules() -> None:
    frame = pd.DataFrame(
        [
            {"event_sampled": 0.0, "ctx_is_expiry_day": 0.0, "ctx_regime_atr_high": 0.0, "ctx_regime_trend_up": 1.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 0.0},
            {"event_sampled": 1.0, "ctx_is_expiry_day": 1.0, "ctx_regime_atr_high": 0.0, "ctx_regime_trend_up": 1.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 0.0},
            {"event_sampled": 1.0, "ctx_is_expiry_day": 0.0, "ctx_regime_atr_high": 1.0, "ctx_regime_trend_up": 1.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 0.0},
            {"event_sampled": 1.0, "ctx_is_expiry_day": 0.0, "ctx_regime_atr_high": 0.0, "ctx_regime_trend_up": 0.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 0.0},
            {"event_sampled": 1.0, "ctx_is_expiry_day": 0.0, "ctx_regime_atr_high": 0.0, "ctx_regime_trend_up": 1.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 0.0},
            {"event_sampled": 1.0, "ctx_is_expiry_day": 0.0, "ctx_regime_atr_high": 0.0, "ctx_regime_trend_up": 0.0, "ctx_regime_trend_down": 0.0, "ctx_regime_expiry_near": 1.0},
        ]
    )
    candidate_filter = {
        "require_event_sampled": True,
        "exclude_expiry_day": True,
        "exclude_regime_atr_high": True,
        "require_tradeable_context": True,
        "allow_near_expiry_context": True,
    }

    filtered, meta = apply_candidate_filter(frame, candidate_filter=candidate_filter, context="test:with_near_expiry")
    assert len(filtered) == 2
    assert meta["rows_before"] == 6
    assert meta["rows_after"] == 2
    assert meta["dropped_by_rule"] == {
        "require_event_sampled": 1,
        "exclude_expiry_day": 1,
        "exclude_regime_atr_high": 1,
        "require_tradeable_context": 1,
    }

    filtered_without_near_expiry, meta_without_near_expiry = apply_candidate_filter(
        frame,
        candidate_filter={**candidate_filter, "allow_near_expiry_context": False},
        context="test:without_near_expiry",
    )
    assert len(filtered_without_near_expiry) == 1
    assert meta_without_near_expiry["dropped_by_rule"]["require_tradeable_context"] == 2


def test_recovery_smoke_persists_filtering_meta(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["scenario"]["event_sampling_mode"] = "cusum"
    payload["scenario"]["candidate_filter"] = {
        "require_event_sampled": True,
        "exclude_expiry_day": True,
        "exclude_regime_atr_high": True,
        "require_tradeable_context": True,
        "allow_near_expiry_context": True,
    }
    payload["scenario"]["meta_gate"]["enabled"] = False
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    summary = run_research(load_and_resolve_manifest(manifest_path, validate_paths=True))
    recipe_summary = json.loads((Path(summary["output_root"]) / "primary_recipes" / "TB_BASE_L1" / "summary.json").read_text(encoding="utf-8"))

    assert "train_filtering_meta" in recipe_summary
    assert "holdout_filtering_meta" in recipe_summary
    assert recipe_summary["train_filtering_meta"]["rows_after"] <= recipe_summary["train_filtering_meta"]["rows_before"]
    assert recipe_summary["holdout_filtering_meta"]["rows_after"] <= recipe_summary["holdout_filtering_meta"]["rows_before"]
    assert recipe_summary["holdout_filtering_meta"]["dropped_by_rule"]["exclude_expiry_day"] >= 0


def test_recovery_run_can_reuse_explicit_output_root_with_resume_primary(tmp_path: Path) -> None:
    model_window_path, holdout_path = build_synthetic_feature_frames(tmp_path)
    manifest_path = build_recovery_smoke_manifest(tmp_path, model_window_path, holdout_path)
    resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
    resolved["scenario"] = dict(resolved["scenario"])
    resolved["scenario"]["resume_primary"] = True
    run_output_root = tmp_path / "artifacts" / "reused_run"

    first_summary = run_research(resolved, run_output_root=run_output_root)
    recipe_summary_path = run_output_root / "primary_recipes" / "TB_BASE_L1" / "summary.json"
    first_recipe_summary = recipe_summary_path.read_text(encoding="utf-8")

    second_summary = run_research(resolved, run_output_root=run_output_root)
    state_lines = (run_output_root / "state.jsonl").read_text(encoding="utf-8").splitlines()

    assert Path(first_summary["output_root"]) == run_output_root
    assert Path(second_summary["output_root"]) == run_output_root
    assert recipe_summary_path.read_text(encoding="utf-8") == first_recipe_summary
    assert any('"event": "primary_recipe_skipped"' in line and '"reason": "resume_primary"' in line for line in state_lines)


def _build_training_cycle_smoke_frame(tmp_path: Path) -> pd.DataFrame:
    model_window_path, _ = build_synthetic_feature_frames(tmp_path)
    features = pd.read_parquet(model_window_path)
    labeled = build_labeled_dataset(
        features=features.copy(),
        cfg=EffectiveLabelConfig(
            horizon_minutes=2,
            return_threshold=0.0,
            use_excursion_gate=False,
            min_favorable_excursion=0.0,
            max_adverse_excursion=0.0,
            stop_loss_pct=0.0005,
            take_profit_pct=0.0010,
            allow_hold_extension=False,
            extension_trigger_profit_pct=0.0,
        ),
    )
    return prepare_snapshot_labeled_frame(labeled, context="smoke:tuning")


def test_training_cycle_smoke_supports_xgb_deep_v1(tmp_path: Path) -> None:
    labeled = _build_training_cycle_smoke_frame(tmp_path)

    result = run_training_cycle_catalog(
        labeled_df=labeled,
        feature_profile="all",
        objective="trade_utility",
        train_days=4,
        valid_days=2,
        test_days=2,
        step_days=2,
        purge_days=0,
        embargo_days=0,
        purge_mode="days",
        embargo_rows=0,
        event_end_col=None,
        random_state=42,
        max_experiments=1,
        preprocess_cfg=PreprocessConfig(),
        label_target="path_tp_sl_resolved_only",
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=0.50,
            pe_threshold=0.50,
            min_profit_factor=0.50,
            max_equity_drawdown_pct=0.50,
            min_trades=1,
            take_profit_pct=0.0010,
            stop_loss_pct=0.0005,
        ),
        model_whitelist=["xgb_deep_v1"],
        feature_set_whitelist=["fo_expiry_aware_v2"],
        fit_all_final_models=False,
        model_n_jobs=2,
    )

    assert result["report"]["best_experiment"]["model"]["name"] == "xgb_deep_v1"
    assert result["model_package"]["selected_model"]["name"] == "xgb_deep_v1"
    assert result["report"]["runtime"]["model_n_jobs"] == 2
    assert result["model_package"]["runtime"]["model_n_jobs"] == 2


def test_training_cycle_smoke_supports_lgbm_large_v1(tmp_path: Path) -> None:
    pytest.importorskip("lightgbm")
    labeled = _build_training_cycle_smoke_frame(tmp_path)

    result = run_training_cycle_catalog(
        labeled_df=labeled,
        feature_profile="all",
        objective="trade_utility",
        train_days=4,
        valid_days=2,
        test_days=2,
        step_days=2,
        purge_days=0,
        embargo_days=0,
        purge_mode="days",
        embargo_rows=0,
        event_end_col=None,
        random_state=42,
        max_experiments=1,
        preprocess_cfg=PreprocessConfig(),
        label_target="path_tp_sl_resolved_only",
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=0.50,
            pe_threshold=0.50,
            min_profit_factor=0.50,
            max_equity_drawdown_pct=0.50,
            min_trades=1,
            take_profit_pct=0.0010,
            stop_loss_pct=0.0005,
        ),
        model_whitelist=["lgbm_large_v1"],
        feature_set_whitelist=["fo_expiry_aware_v2"],
        fit_all_final_models=False,
    )

    assert result["report"]["best_experiment"]["model"]["name"] == "lgbm_large_v1"
    assert result["model_package"]["selected_model"]["name"] == "lgbm_large_v1"
