from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, f1_score, precision_score, recall_score, roc_auc_score

from .dataset_windowing.frames import filter_trade_dates, load_feature_frame, window_metadata
from .inference_contract import predict_probabilities_from_frame
from .labeling import EffectiveLabelConfig, build_label_lineage, build_labeled_dataset, prepare_snapshot_labeled_frame
from .model_search import run_training_cycle_catalog


DEFAULT_OUT_ROOT = "ml_pipeline_2/artifacts/research"
DEFAULT_RUN_NAME = "move_detector_quick"
DEFAULT_FEATURE_SET = "fo_expiry_aware_v2"


def _timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a quick Stage 1 move/no-move detector training cycle.")
    parser.add_argument("--config", help="Optional JSON config for the move detector lane")
    parser.add_argument("--print-resolved-config", action="store_true", help="Print the resolved config JSON and exit")
    parser.add_argument("--resume", action="store_true", help="Resume from an existing run directory")
    parser.add_argument("--run-dir", help="Explicit run directory. Required for --resume")
    parser.add_argument("--model-window-features", help="Parquet path for the model-window feature snapshot")
    parser.add_argument("--holdout-features", help="Parquet path for the holdout feature snapshot")
    parser.add_argument("--train-start", help="Train window start date (YYYY-MM-DD)")
    parser.add_argument("--train-end", help="Train window end date (YYYY-MM-DD)")
    parser.add_argument("--holdout-start", help="Holdout window start date (YYYY-MM-DD)")
    parser.add_argument("--holdout-end", help="Holdout window end date (YYYY-MM-DD)")
    parser.add_argument("--horizon-minutes", type=int, help="Vertical barrier horizon in minutes")
    parser.add_argument("--atr-multiplier", type=float, help="Symmetric ATR multiplier for up/down barriers")
    parser.add_argument("--fallback-barrier-pct", type=float, help="Fallback fixed barrier percent if ATR reference is unavailable")
    parser.add_argument("--min-entry-time", help="Ignore candidate rows before this local time (HH:MM)")
    parser.add_argument("--feature-profile", help="Feature profile for selection")
    parser.add_argument("--feature-set", help="Single feature set to train")
    parser.add_argument("--feature-sets", help="Comma-separated feature sets to search")
    parser.add_argument("--model-name", help="Single model to train")
    parser.add_argument("--models", help="Comma-separated model names to search")
    parser.add_argument("--max-experiments", type=int, help="Optional cap on evaluated experiments")
    parser.add_argument("--objective", choices=("brier", "rmse"), help="Binary move objective")
    parser.add_argument("--train-days", type=int, help="Walk-forward train days")
    parser.add_argument("--valid-days", type=int, help="Walk-forward valid days")
    parser.add_argument("--test-days", type=int, help="Walk-forward test days")
    parser.add_argument("--step-days", type=int, help="Walk-forward step days")
    parser.add_argument("--threshold-grid", help="Comma-separated holdout thresholds")
    parser.add_argument("--out-root", help="Artifact root")
    parser.add_argument("--run-name", help="Artifact run name prefix")
    return parser


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path_value(value: Any, *, base_dir: Path) -> str:
    txt = str(value or "").strip()
    if not txt:
        return ""
    path = Path(txt)
    return str(path.resolve() if path.is_absolute() else (base_dir / path).resolve())


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _append_state(run_dir: Path, event: str, **data: Any) -> None:
    path = run_dir / "state.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts_utc": _utc_now(), "event": str(event)}
    payload.update(data)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def _load_config(path: Optional[str]) -> tuple[Dict[str, Any], Path]:
    if path is None or not str(path).strip():
        return {}, Path.cwd().resolve()
    config_path = Path(path).resolve()
    return _read_json(config_path), config_path.parent


def _pick(cli_value: Any, config_value: Any, default_value: Any) -> Any:
    return cli_value if cli_value is not None else (config_value if config_value is not None else default_value)


def _parse_threshold_grid(value: Any) -> List[float]:
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if value is None:
        return [0.50, 0.55, 0.60, 0.65]
    return [float(part) for part in str(value).split(",") if str(part).strip()]


def _parse_name_list(value: Any, default_items: Sequence[str]) -> List[str]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or [str(item) for item in default_items]
    if value is None:
        return [str(item) for item in default_items]
    items = [part.strip() for part in str(value).split(",") if part.strip()]
    return items or [str(item) for item in default_items]


def _resolve_config(args: argparse.Namespace) -> Dict[str, Any]:
    payload, config_dir = _load_config(args.config)
    inputs_cfg = dict(payload.get("inputs") or {})
    windows_cfg = dict(payload.get("windows") or {})
    label_cfg = dict(payload.get("label") or {})
    training_cfg = dict(payload.get("training") or {})
    cv_cfg = dict(training_cfg.get("cv") or {})
    outputs_cfg = dict(payload.get("outputs") or {})
    cli_base_dir = Path.cwd().resolve()

    resolved = {
        "inputs": {
            "model_window_features": (
                _resolve_path_value(args.model_window_features, base_dir=cli_base_dir)
                if args.model_window_features is not None
                else _resolve_path_value(inputs_cfg.get("model_window_features"), base_dir=config_dir)
            ),
            "holdout_features": (
                _resolve_path_value(args.holdout_features, base_dir=cli_base_dir)
                if args.holdout_features is not None
                else _resolve_path_value(inputs_cfg.get("holdout_features"), base_dir=config_dir)
            ),
        },
        "windows": {
            "train": {
                "start": str(_pick(args.train_start, ((windows_cfg.get("train") or {}).get("start")), "")).strip(),
                "end": str(_pick(args.train_end, ((windows_cfg.get("train") or {}).get("end")), "")).strip(),
            },
            "holdout": {
                "start": str(_pick(args.holdout_start, ((windows_cfg.get("holdout") or {}).get("start")), "")).strip(),
                "end": str(_pick(args.holdout_end, ((windows_cfg.get("holdout") or {}).get("end")), "")).strip(),
            },
        },
        "label": {
            "horizon_minutes": int(_pick(args.horizon_minutes, label_cfg.get("horizon_minutes"), 20)),
            "atr_multiplier": float(_pick(args.atr_multiplier, label_cfg.get("atr_multiplier"), 1.75)),
            "fallback_barrier_pct": float(_pick(args.fallback_barrier_pct, label_cfg.get("fallback_barrier_pct"), 0.0025)),
            "min_entry_time": str(_pick(args.min_entry_time, label_cfg.get("min_entry_time"), "09:20")).strip(),
        },
        "training": {
            "feature_profile": str(_pick(args.feature_profile, training_cfg.get("feature_profile"), "all")).strip(),
            "feature_sets": _parse_name_list(_pick(args.feature_sets, training_cfg.get("feature_sets"), None), default_items=[str(_pick(args.feature_set, training_cfg.get("feature_set"), DEFAULT_FEATURE_SET)).strip()]),
            "models": _parse_name_list(_pick(args.models, training_cfg.get("models"), None), default_items=[str(_pick(args.model_name, training_cfg.get("model_name"), "xgb_shallow")).strip()]),
            "max_experiments": _pick(args.max_experiments, training_cfg.get("max_experiments"), None),
            "objective": str(_pick(args.objective, training_cfg.get("objective"), "brier")).strip(),
            "threshold_grid": _parse_threshold_grid(_pick(args.threshold_grid, training_cfg.get("threshold_grid"), None)),
            "cv": {
                "train_days": int(_pick(args.train_days, cv_cfg.get("train_days"), 60)),
                "valid_days": int(_pick(args.valid_days, cv_cfg.get("valid_days"), 15)),
                "test_days": int(_pick(args.test_days, cv_cfg.get("test_days"), 15)),
                "step_days": int(_pick(args.step_days, cv_cfg.get("step_days"), 15)),
            },
        },
        "outputs": {
            "out_root": (
                _resolve_path_value(args.out_root, base_dir=cli_base_dir)
                if args.out_root is not None
                else _resolve_path_value(outputs_cfg.get("out_root"), base_dir=config_dir)
                if outputs_cfg.get("out_root") is not None
                else _resolve_path_value(DEFAULT_OUT_ROOT, base_dir=cli_base_dir)
            ),
            "run_name": str(_pick(args.run_name, outputs_cfg.get("run_name"), DEFAULT_RUN_NAME)).strip(),
            "run_dir": (
                _resolve_path_value(args.run_dir, base_dir=cli_base_dir)
                if args.run_dir is not None
                else _resolve_path_value(outputs_cfg.get("run_dir"), base_dir=config_dir)
            ),
            "resume": bool(args.resume or bool(outputs_cfg.get("resume", False))),
        },
    }

    missing = [
        key
        for key, value in {
            "inputs.model_window_features": resolved["inputs"]["model_window_features"],
            "inputs.holdout_features": resolved["inputs"]["holdout_features"],
            "windows.train.start": resolved["windows"]["train"]["start"],
            "windows.train.end": resolved["windows"]["train"]["end"],
            "windows.holdout.start": resolved["windows"]["holdout"]["start"],
            "windows.holdout.end": resolved["windows"]["holdout"]["end"],
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"missing required config values: {missing}")
    if resolved["outputs"]["resume"] and not resolved["outputs"]["run_dir"]:
        raise ValueError("--resume requires --run-dir or outputs.run_dir in config")
    return resolved


def _prepare_run_dir(resolved: Dict[str, Any]) -> Path:
    outputs = dict(resolved["outputs"])
    explicit = str(outputs.get("run_dir") or "").strip()
    if explicit:
        run_dir = Path(explicit).resolve()
    else:
        run_dir = (Path(outputs["out_root"]) / f"{outputs['run_name']}_{_timestamp_suffix()}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _validate_or_persist_config(run_dir: Path, resolved: Dict[str, Any]) -> None:
    path = run_dir / "resolved_config.json"
    if path.exists():
        existing = _read_json(path)
        if json.dumps(existing, sort_keys=True) != json.dumps(resolved, sort_keys=True):
            raise ValueError(f"resolved config mismatch for resume run: {path}")
    else:
        _write_json(path, resolved)


def _filter_min_entry_time(frame: pd.DataFrame, *, min_entry_time: str) -> pd.DataFrame:
    out = frame.copy()
    cutoff = pd.Timestamp(f"2000-01-01 {str(min_entry_time).strip()}").time()
    mask = out["timestamp"].dt.time >= cutoff
    return out.loc[mask].copy().sort_values("timestamp").reset_index(drop=True)


def _holdout_metrics(frame: pd.DataFrame, probs: pd.DataFrame, thresholds: Sequence[float]) -> Dict[str, object]:
    valid = pd.to_numeric(frame["move_label_valid"], errors="coerce").fillna(0.0) == 1.0
    labels = pd.to_numeric(frame.loc[valid, "move_label"], errors="coerce")
    move_prob = pd.to_numeric(probs.loc[valid, "move_prob"], errors="coerce")
    usable = labels.notna() & move_prob.notna()
    y = labels.loc[usable].astype(int).to_numpy()
    p = move_prob.loc[usable].astype(float).to_numpy()
    has_both = len(np.unique(y)) >= 2
    summary: Dict[str, object] = {
        "rows_total": int(len(frame)),
        "rows_valid": int(len(y)),
        "positive_rate": float(np.mean(y)) if len(y) else 0.0,
        "roc_auc": float(roc_auc_score(y, p)) if has_both else None,
        "pr_auc": float(average_precision_score(y, p)) if has_both else None,
        "brier": float(brier_score_loss(y, p)) if len(y) else None,
        "thresholds": [],
    }
    for threshold in thresholds:
        pred = (p >= float(threshold)).astype(int)
        summary["thresholds"].append(
            {
                "threshold": float(threshold),
                "predicted_positive_count": int(np.sum(pred)),
                "prediction_rate": float(np.mean(pred)) if len(pred) else 0.0,
                "precision": float(precision_score(y, pred, zero_division=0)) if len(y) else 0.0,
                "recall": float(recall_score(y, pred, zero_division=0)) if len(y) else 0.0,
                "f1": float(f1_score(y, pred, zero_division=0)) if len(y) else 0.0,
            }
        )
    return summary


def _window_features(run_dir: Path, resolved: Dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    model_window_path = run_dir / "model_window_features_windowed.parquet"
    holdout_path = run_dir / "holdout_features_windowed.parquet"
    if model_window_path.exists() and holdout_path.exists():
        _append_state(run_dir, "windowed_features_reused", model_window=str(model_window_path), holdout=str(holdout_path))
        return pd.read_parquet(model_window_path), pd.read_parquet(holdout_path)
    _append_state(run_dir, "windowed_features_start")
    model_window_features = load_feature_frame(Path(resolved["inputs"]["model_window_features"]))
    holdout_features = load_feature_frame(Path(resolved["inputs"]["holdout_features"]))
    model_window_features = filter_trade_dates(model_window_features, resolved["windows"]["train"]["start"], resolved["windows"]["train"]["end"])
    holdout_features = filter_trade_dates(holdout_features, resolved["windows"]["holdout"]["start"], resolved["windows"]["holdout"]["end"])
    model_window_features.to_parquet(model_window_path, index=False)
    holdout_features.to_parquet(holdout_path, index=False)
    _append_state(run_dir, "windowed_features_done", model_window_rows=int(len(model_window_features)), holdout_rows=int(len(holdout_features)))
    return model_window_features, holdout_features


def _label_frames(run_dir: Path, resolved: Dict[str, Any], label_cfg: EffectiveLabelConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    model_window_labeled_path = run_dir / "model_window_labeled.parquet"
    holdout_labeled_path = run_dir / "holdout_labeled.parquet"
    if model_window_labeled_path.exists() and holdout_labeled_path.exists():
        _append_state(run_dir, "labeling_reused", model_window=str(model_window_labeled_path), holdout=str(holdout_labeled_path))
        return pd.read_parquet(model_window_labeled_path), pd.read_parquet(holdout_labeled_path)
    model_window_features, holdout_features = _window_features(run_dir, resolved)
    _append_state(run_dir, "labeling_start")
    model_window_labeled = prepare_snapshot_labeled_frame(
        build_labeled_dataset(model_window_features, cfg=label_cfg),
        context="move_detector_quick:model_window",
    )
    holdout_labeled = prepare_snapshot_labeled_frame(
        build_labeled_dataset(holdout_features, cfg=label_cfg),
        context="move_detector_quick:holdout",
    )
    model_window_labeled = _filter_min_entry_time(model_window_labeled, min_entry_time=resolved["label"]["min_entry_time"])
    holdout_labeled = _filter_min_entry_time(holdout_labeled, min_entry_time=resolved["label"]["min_entry_time"])
    model_window_labeled.to_parquet(model_window_labeled_path, index=False)
    holdout_labeled.to_parquet(holdout_labeled_path, index=False)
    label_lineage = {
        "model_window": build_label_lineage(model_window_labeled, label_cfg),
        "holdout": build_label_lineage(holdout_labeled, label_cfg),
    }
    _write_json(run_dir / "label_lineage.json", label_lineage)
    _append_state(run_dir, "labeling_done", model_window_rows=int(len(model_window_labeled)), holdout_rows=int(len(holdout_labeled)))
    return model_window_labeled, holdout_labeled


def _train_or_resume(run_dir: Path, resolved: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    training_report_path = run_dir / "training_report.json"
    model_package_path = run_dir / "model.joblib"
    if training_report_path.exists() and model_package_path.exists():
        _append_state(run_dir, "training_reused", training_report=str(training_report_path), model_package=str(model_package_path))
        return _read_json(training_report_path), dict(joblib.load(model_package_path))
    label_cfg = EffectiveLabelConfig(
        horizon_minutes=int(resolved["label"]["horizon_minutes"]),
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        stop_loss_pct=float(resolved["label"]["fallback_barrier_pct"]),
        take_profit_pct=float(resolved["label"]["fallback_barrier_pct"]),
        allow_hold_extension=False,
        extension_trigger_profit_pct=0.0,
        barrier_mode="atr_scaled",
        atr_reference_col="osc_atr_ratio",
        atr_tp_multiplier=float(resolved["label"]["atr_multiplier"]),
        atr_sl_multiplier=float(resolved["label"]["atr_multiplier"]),
        atr_clip_min_factor=0.5,
        atr_clip_max_factor=1.5,
        neutral_policy="exclude_from_primary",
        event_sampling_mode="none",
        event_signal_col="opt_flow_ce_pe_oi_diff",
        event_end_ts_mode="first_touch_or_vertical",
    )
    model_window_labeled, _ = _label_frames(run_dir, resolved, label_cfg)
    _append_state(run_dir, "training_start")
    training_result = run_training_cycle_catalog(
        labeled_df=model_window_labeled,
        feature_profile=str(resolved["training"]["feature_profile"]),
        objective=str(resolved["training"]["objective"]),
        train_days=int(resolved["training"]["cv"]["train_days"]),
        valid_days=int(resolved["training"]["cv"]["valid_days"]),
        test_days=int(resolved["training"]["cv"]["test_days"]),
        step_days=int(resolved["training"]["cv"]["step_days"]),
        purge_days=0,
        embargo_days=0,
        purge_mode="days",
        embargo_rows=0,
        event_end_col="move_event_end_ts",
        random_state=42,
        max_experiments=(int(resolved["training"]["max_experiments"]) if resolved["training"]["max_experiments"] is not None else None),
        label_target="move_barrier_hit",
        model_whitelist=list(resolved["training"]["models"]),
        feature_set_whitelist=list(resolved["training"]["feature_sets"]),
        fit_all_final_models=False,
    )
    training_report = dict(training_result["report"])
    model_package = dict(training_result["model_package"])
    _write_json(training_report_path, training_report)
    joblib.dump(model_package, model_package_path)
    _append_state(run_dir, "training_done", training_report=str(training_report_path), model_package=str(model_package_path))
    return training_report, model_package


def _predict_or_resume(run_dir: Path, resolved: Dict[str, Any], model_package: Dict[str, Any], label_cfg: EffectiveLabelConfig) -> tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    holdout_labeled_path = run_dir / "holdout_labeled.parquet"
    holdout_predictions_csv = run_dir / "holdout_predictions.csv"
    holdout_probabilities_parquet = run_dir / "holdout_probabilities.parquet"
    input_contract_path = run_dir / "input_contract.json"
    if holdout_labeled_path.exists() and holdout_predictions_csv.exists() and holdout_probabilities_parquet.exists() and input_contract_path.exists():
        _append_state(run_dir, "holdout_scoring_reused", predictions=str(holdout_predictions_csv))
        return pd.read_parquet(holdout_labeled_path), pd.read_parquet(holdout_probabilities_parquet), _read_json(input_contract_path)
    _, holdout_labeled = _label_frames(run_dir, resolved, label_cfg)
    _append_state(run_dir, "holdout_scoring_start")
    probs, input_contract = predict_probabilities_from_frame(
        holdout_labeled,
        model_package,
        missing_policy_override="error",
        context="move_detector_quick:holdout",
    )
    predictions = holdout_labeled.loc[:, ["timestamp", "trade_date", "move_label", "move_label_valid", "move_first_hit_side", "move_event_end_ts"]].copy()
    predictions["move_prob"] = pd.to_numeric(probs["move_prob"], errors="coerce")
    predictions.to_csv(holdout_predictions_csv, index=False)
    probs.to_parquet(holdout_probabilities_parquet, index=False)
    _write_json(input_contract_path, input_contract)
    _append_state(run_dir, "holdout_scoring_done", predictions=str(holdout_predictions_csv))
    return holdout_labeled, probs, input_contract


def _final_summary(
    run_dir: Path,
    resolved: Dict[str, Any],
    label_cfg: EffectiveLabelConfig,
    training_report: Dict[str, Any],
    input_contract: Dict[str, Any],
    holdout_labeled: pd.DataFrame,
    probs: pd.DataFrame,
) -> Dict[str, Any]:
    thresholds = list(resolved["training"]["threshold_grid"])
    model_window_labeled = pd.read_parquet(run_dir / "model_window_labeled.parquet")
    summary = {
        "status": "completed",
        "created_at_utc": _utc_now(),
        "paths": {
            "model_window_features": str(Path(resolved["inputs"]["model_window_features"]).resolve()),
            "holdout_features": str(Path(resolved["inputs"]["holdout_features"]).resolve()),
            "windowed_model_window_features": str((run_dir / "model_window_features_windowed.parquet").resolve()),
            "windowed_holdout_features": str((run_dir / "holdout_features_windowed.parquet").resolve()),
            "model_window_labeled": str((run_dir / "model_window_labeled.parquet").resolve()),
            "holdout_labeled": str((run_dir / "holdout_labeled.parquet").resolve()),
            "training_report": str((run_dir / "training_report.json").resolve()),
            "model_package": str((run_dir / "model.joblib").resolve()),
            "holdout_predictions": str((run_dir / "holdout_predictions.csv").resolve()),
            "holdout_probabilities": str((run_dir / "holdout_probabilities.parquet").resolve()),
            "label_lineage": str((run_dir / "label_lineage.json").resolve()),
            "resolved_config": str((run_dir / "resolved_config.json").resolve()),
            "state": str((run_dir / "state.jsonl").resolve()),
        },
        "config": {
            "label": asdict(label_cfg),
            "feature_profile": str(resolved["training"]["feature_profile"]),
            "feature_sets": list(resolved["training"]["feature_sets"]),
            "models": list(resolved["training"]["models"]),
            "max_experiments": resolved["training"]["max_experiments"],
            "objective": str(resolved["training"]["objective"]),
            "min_entry_time": str(resolved["label"]["min_entry_time"]),
            "threshold_grid": thresholds,
            "cv": dict(resolved["training"]["cv"]),
            "resume": bool(resolved["outputs"]["resume"]),
            "run_name": str(resolved["outputs"]["run_name"]),
        },
        "windows": {
            "train": window_metadata(model_window_labeled, start_day=resolved["windows"]["train"]["start"], end_day=resolved["windows"]["train"]["end"]),
            "holdout": window_metadata(holdout_labeled, start_day=resolved["windows"]["holdout"]["start"], end_day=resolved["windows"]["holdout"]["end"]),
        },
        "input_contract": input_contract,
        "training_report": training_report,
        "holdout_metrics": _holdout_metrics(holdout_labeled, probs, thresholds),
        "output_root": str(run_dir),
    }
    _write_json(run_dir / "summary.json", summary)
    _append_state(run_dir, "summary_done", summary_path=str((run_dir / "summary.json").resolve()))
    return summary


def run_move_detector_quick(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    args = _build_parser().parse_args(argv)
    resolved = _resolve_config(args)
    run_dir = _prepare_run_dir(resolved)
    _validate_or_persist_config(run_dir, resolved)
    summary_path = run_dir / "summary.json"
    if bool(resolved["outputs"]["resume"]) and summary_path.exists():
        _append_state(run_dir, "job_resume_complete", summary_path=str(summary_path.resolve()))
        return _read_json(summary_path)
    _append_state(run_dir, "job_start", run_dir=str(run_dir), resume=bool(resolved["outputs"]["resume"]))
    label_cfg = EffectiveLabelConfig(
        horizon_minutes=int(resolved["label"]["horizon_minutes"]),
        return_threshold=0.0,
        use_excursion_gate=False,
        min_favorable_excursion=0.0,
        max_adverse_excursion=0.0,
        stop_loss_pct=float(resolved["label"]["fallback_barrier_pct"]),
        take_profit_pct=float(resolved["label"]["fallback_barrier_pct"]),
        allow_hold_extension=False,
        extension_trigger_profit_pct=0.0,
        barrier_mode="atr_scaled",
        atr_reference_col="osc_atr_ratio",
        atr_tp_multiplier=float(resolved["label"]["atr_multiplier"]),
        atr_sl_multiplier=float(resolved["label"]["atr_multiplier"]),
        atr_clip_min_factor=0.5,
        atr_clip_max_factor=1.5,
        neutral_policy="exclude_from_primary",
        event_sampling_mode="none",
        event_signal_col="opt_flow_ce_pe_oi_diff",
        event_end_ts_mode="first_touch_or_vertical",
    )
    _window_features(run_dir, resolved)
    _label_frames(run_dir, resolved, label_cfg)
    training_report, model_package = _train_or_resume(run_dir, resolved)
    holdout_labeled, probs, input_contract = _predict_or_resume(run_dir, resolved, model_package, label_cfg)
    summary = _final_summary(run_dir, resolved, label_cfg, training_report, input_contract, holdout_labeled, probs)
    _append_state(run_dir, "job_done", status=str(summary.get("status", "completed")))
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    resolved = _resolve_config(args)
    if args.print_resolved_config:
        print(json.dumps(resolved, indent=2))
        return 0
    summary = run_move_detector_quick(argv)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
