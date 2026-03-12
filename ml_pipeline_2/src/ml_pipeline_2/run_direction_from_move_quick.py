from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, f1_score, precision_score, recall_score, roc_auc_score

from .inference_contract import predict_probabilities_from_frame
from .model_search import run_training_cycle_catalog


DEFAULT_OUT_ROOT = "ml_pipeline_2/artifacts/research"
DEFAULT_RUN_NAME = "direction_from_move_quick"
DEFAULT_FEATURE_SET = "fo_expiry_aware_v2"


def _timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a quick Stage 2 direction model conditioned on a completed Stage 1 move detector run.")
    parser.add_argument("--config", help="Optional JSON config for the direction lane")
    parser.add_argument("--print-resolved-config", action="store_true", help="Print the resolved config JSON and exit")
    parser.add_argument("--resume", action="store_true", help="Resume from an existing run directory")
    parser.add_argument("--run-dir", help="Explicit run directory. Required for --resume")
    parser.add_argument("--stage1-run-dir", help="Completed Stage 1 move-detector run directory")
    parser.add_argument("--feature-profile", help="Feature profile for selection")
    parser.add_argument("--feature-set", help="Single feature set to train")
    parser.add_argument("--feature-sets", help="Comma-separated feature sets to search")
    parser.add_argument("--model-name", help="Single model to train")
    parser.add_argument("--models", help="Comma-separated model names to search")
    parser.add_argument("--max-experiments", type=int, help="Optional cap on evaluated experiments")
    parser.add_argument("--objective", choices=("brier", "rmse"), help="Direction objective")
    parser.add_argument("--train-days", type=int, help="Walk-forward train days")
    parser.add_argument("--valid-days", type=int, help="Walk-forward valid days")
    parser.add_argument("--test-days", type=int, help="Walk-forward test days")
    parser.add_argument("--step-days", type=int, help="Walk-forward step days")
    parser.add_argument("--move-threshold", type=float, help="Stage 1 move threshold for combined holdout gating")
    parser.add_argument("--direction-threshold-grid", help="Comma-separated Stage 2 direction thresholds")
    parser.add_argument("--cost-per-trade", type=float, help="Cost deducted from chosen-side return in combined holdout summary")
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


def _parse_threshold_grid(value: Any, default_grid: Sequence[float]) -> List[float]:
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if value is None:
        return [float(item) for item in default_grid]
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
    training_cfg = dict(payload.get("training") or {})
    cv_cfg = dict(training_cfg.get("cv") or {})
    outputs_cfg = dict(payload.get("outputs") or {})
    gating_cfg = dict(payload.get("gating") or {})
    cli_base_dir = Path.cwd().resolve()
    resolved = {
        "inputs": {
            "stage1_run_dir": (
                _resolve_path_value(args.stage1_run_dir, base_dir=cli_base_dir)
                if args.stage1_run_dir is not None
                else _resolve_path_value(inputs_cfg.get("stage1_run_dir"), base_dir=config_dir)
            ),
        },
        "training": {
            "feature_profile": str(_pick(args.feature_profile, training_cfg.get("feature_profile"), "all")).strip(),
            "feature_sets": _parse_name_list(_pick(args.feature_sets, training_cfg.get("feature_sets"), None), default_items=[str(_pick(args.feature_set, training_cfg.get("feature_set"), DEFAULT_FEATURE_SET)).strip()]),
            "models": _parse_name_list(_pick(args.models, training_cfg.get("models"), None), default_items=[str(_pick(args.model_name, training_cfg.get("model_name"), "xgb_shallow")).strip()]),
            "max_experiments": (_pick(args.max_experiments, training_cfg.get("max_experiments"), None)),
            "objective": str(_pick(args.objective, training_cfg.get("objective"), "brier")).strip(),
            "cv": {
                "train_days": int(_pick(args.train_days, cv_cfg.get("train_days"), 60)),
                "valid_days": int(_pick(args.valid_days, cv_cfg.get("valid_days"), 15)),
                "test_days": int(_pick(args.test_days, cv_cfg.get("test_days"), 15)),
                "step_days": int(_pick(args.step_days, cv_cfg.get("step_days"), 15)),
            },
        },
        "gating": {
            "move_threshold": float(_pick(args.move_threshold, gating_cfg.get("move_threshold"), 0.60)),
            "direction_threshold_grid": _parse_threshold_grid(_pick(args.direction_threshold_grid, gating_cfg.get("direction_threshold_grid"), None), default_grid=[0.55, 0.60, 0.65]),
            "cost_per_trade": float(_pick(args.cost_per_trade, gating_cfg.get("cost_per_trade"), 0.0006)),
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
    if not resolved["inputs"]["stage1_run_dir"]:
        raise ValueError("missing required config value: inputs.stage1_run_dir")
    if resolved["outputs"]["resume"] and not resolved["outputs"]["run_dir"]:
        raise ValueError("--resume requires --run-dir or outputs.run_dir in config")
    return resolved


def _prepare_run_dir(resolved: Dict[str, Any]) -> Path:
    explicit = str((resolved.get("outputs") or {}).get("run_dir") or "").strip()
    if explicit:
        run_dir = Path(explicit).resolve()
    else:
        outputs = dict(resolved["outputs"])
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


def _ensure_stage1_artifacts(stage1_run_dir: Path) -> Dict[str, Path]:
    required = {
        "summary": stage1_run_dir / "summary.json",
        "model_window_labeled": stage1_run_dir / "model_window_labeled.parquet",
        "holdout_labeled": stage1_run_dir / "holdout_labeled.parquet",
        "model_package": stage1_run_dir / "model.joblib",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise ValueError(f"stage1 run dir missing required artifacts: {missing}")
    optional = {
        "holdout_probabilities": stage1_run_dir / "holdout_probabilities.parquet",
        "holdout_predictions": stage1_run_dir / "holdout_predictions.csv",
    }
    out = dict(required)
    out.update(optional)
    return out


def _load_stage1_frames(stage1_run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    model_window = pd.read_parquet(stage1_run_dir / "model_window_labeled.parquet")
    holdout = pd.read_parquet(stage1_run_dir / "holdout_labeled.parquet")
    return model_window, holdout


def _score_stage1_holdout(stage1_run_dir: Path, holdout_labeled: pd.DataFrame) -> pd.DataFrame:
    probabilities_path = stage1_run_dir / "holdout_probabilities.parquet"
    if probabilities_path.exists():
        probs = pd.read_parquet(probabilities_path)
        if "move_prob" in probs.columns and len(probs) == len(holdout_labeled):
            return probs.loc[:, ["move_prob"]].reset_index(drop=True)
    predictions_path = stage1_run_dir / "holdout_predictions.csv"
    if predictions_path.exists():
        preds = pd.read_csv(predictions_path)
        if "move_prob" in preds.columns and len(preds) == len(holdout_labeled):
            return preds.loc[:, ["move_prob"]].reset_index(drop=True)
    stage1_package = dict(joblib.load(stage1_run_dir / "model.joblib"))
    probs, _ = predict_probabilities_from_frame(holdout_labeled, stage1_package, context="direction_from_move_quick:stage1_holdout")
    if "move_prob" not in probs.columns:
        raise ValueError("stage1 model package did not return move_prob")
    return probs.loc[:, ["move_prob"]].reset_index(drop=True)


def _direction_quality(frame: pd.DataFrame, probs: pd.DataFrame, thresholds: Sequence[float]) -> Dict[str, object]:
    valid = pd.to_numeric(frame.get("move_label_valid"), errors="coerce").fillna(0.0) == 1.0
    moved = pd.to_numeric(frame.get("move_label"), errors="coerce").fillna(0.0) == 1.0
    direction = frame.loc[valid & moved, "move_first_hit_side"].astype(str).str.strip().str.lower()
    y = np.where(direction == "up", 1, np.where(direction == "down", 0, -1))
    usable = y >= 0
    p = pd.to_numeric(probs.loc[valid & moved, "direction_up_prob"], errors="coerce").to_numpy(dtype=float)
    y = y[usable]
    p = p[usable]
    has_both = len(np.unique(y)) >= 2
    out: Dict[str, object] = {
        "rows_move_positive": int(len(y)),
        "up_rate": float(np.mean(y)) if len(y) else 0.0,
        "roc_auc": float(roc_auc_score(y, p)) if has_both else None,
        "pr_auc": float(average_precision_score(y, p)) if has_both else None,
        "brier": float(brier_score_loss(y, p)) if len(y) else None,
        "thresholds": [],
    }
    for threshold in thresholds:
        pred = np.where(p >= float(threshold), 1, np.where(p <= (1.0 - float(threshold)), 0, -1))
        taken = pred >= 0
        out["thresholds"].append(
            {
                "threshold": float(threshold),
                "trades": int(np.sum(taken)),
                "precision": float(np.mean(pred[taken] == y[taken])) if np.any(taken) else 0.0,
                "recall": float(np.mean((pred == y) & (pred >= 0))) if len(y) else 0.0,
                "f1": float(f1_score(y[taken], pred[taken], zero_division=0)) if np.any(taken) and len(np.unique(pred[taken])) >= 1 else 0.0,
                "up_share": float(np.mean(pred[taken] == 1)) if np.any(taken) else 0.0,
            }
        )
    return out


def _profit_factor(net_returns: Sequence[float]) -> float:
    gains = sum(value for value in net_returns if value > 0.0)
    losses = abs(sum(value for value in net_returns if value < 0.0))
    if gains <= 0.0 and losses <= 0.0:
        return 0.0
    if losses <= 0.0:
        return float("inf")
    return float(gains / losses)


def _combined_holdout_summary(
    frame: pd.DataFrame,
    stage1_probs: pd.DataFrame,
    direction_probs: pd.DataFrame,
    *,
    move_threshold: float,
    direction_thresholds: Sequence[float],
    cost_per_trade: float,
) -> Dict[str, object]:
    move_prob = pd.to_numeric(stage1_probs["move_prob"], errors="coerce").to_numpy(dtype=float)
    direction_prob = pd.to_numeric(direction_probs["direction_up_prob"], errors="coerce").to_numpy(dtype=float)
    actual_side = frame["move_first_hit_side"].astype(str).str.strip().str.lower().to_numpy(dtype=object)
    move_label = pd.to_numeric(frame["move_label"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    long_ret = pd.to_numeric(frame["long_forward_return"], errors="coerce").to_numpy(dtype=float)
    short_ret = pd.to_numeric(frame["short_forward_return"], errors="coerce").to_numpy(dtype=float)
    rows_total = int(len(frame))
    summaries: List[Dict[str, object]] = []
    for threshold in direction_thresholds:
        actions = np.full(rows_total, "HOLD", dtype=object)
        move_ok = move_prob >= float(move_threshold)
        actions[move_ok & (direction_prob >= float(threshold))] = "BUY_UP"
        actions[move_ok & (direction_prob <= (1.0 - float(threshold)))] = "BUY_DOWN"
        taken = actions != "HOLD"
        trades = int(np.sum(taken))
        net_returns: List[float] = []
        correct = 0
        up_trades = 0
        down_trades = 0
        false_move_trades = 0
        for idx, action in enumerate(actions):
            if action == "BUY_UP":
                up_trades += 1
                if actual_side[idx] == "up":
                    correct += 1
                if move_label[idx] <= 0.0:
                    false_move_trades += 1
                if np.isfinite(long_ret[idx]):
                    net_returns.append(float(long_ret[idx]) - float(cost_per_trade))
            elif action == "BUY_DOWN":
                down_trades += 1
                if actual_side[idx] == "down":
                    correct += 1
                if move_label[idx] <= 0.0:
                    false_move_trades += 1
                if np.isfinite(short_ret[idx]):
                    net_returns.append(float(short_ret[idx]) - float(cost_per_trade))
        summaries.append(
            {
                "direction_threshold": float(threshold),
                "move_threshold": float(move_threshold),
                "trades": trades,
                "up_trades": int(up_trades),
                "down_trades": int(down_trades),
                "hold_count": int(rows_total - trades),
                "up_share": float(up_trades / trades) if trades else 0.0,
                "down_share": float(down_trades / trades) if trades else 0.0,
                "precision": float(correct / trades) if trades else 0.0,
                "false_move_trade_count": int(false_move_trades),
                "net_return_sum": float(np.sum(net_returns)) if net_returns else 0.0,
                "mean_net_return_per_trade": float(np.mean(net_returns)) if net_returns else 0.0,
                "profit_factor": float(_profit_factor(net_returns)),
            }
        )
    return {
        "rows_total": rows_total,
        "move_threshold": float(move_threshold),
        "cost_per_trade": float(cost_per_trade),
        "thresholds": summaries,
    }


def _write_combined_trade_reports(
    run_dir: Path,
    frame: pd.DataFrame,
    stage1_probs: pd.DataFrame,
    direction_probs: pd.DataFrame,
    *,
    move_threshold: float,
    direction_thresholds: Sequence[float],
    cost_per_trade: float,
) -> Dict[float, str]:
    reports_root = run_dir / "combined_holdout"
    reports_root.mkdir(parents=True, exist_ok=True)
    move_prob = pd.to_numeric(stage1_probs["move_prob"], errors="coerce").to_numpy(dtype=float)
    direction_prob = pd.to_numeric(direction_probs["direction_up_prob"], errors="coerce").to_numpy(dtype=float)
    entry_price = pd.to_numeric(frame["long_entry_price"], errors="coerce").to_numpy(dtype=float)
    horizon_end_price = pd.to_numeric(frame["long_exit_price"], errors="coerce").to_numpy(dtype=float)
    long_ret = pd.to_numeric(frame["long_forward_return"], errors="coerce").to_numpy(dtype=float)
    short_ret = pd.to_numeric(frame["short_forward_return"], errors="coerce").to_numpy(dtype=float)
    horizon_end_points = np.where(
        np.isfinite(entry_price) & np.isfinite(horizon_end_price),
        horizon_end_price - entry_price,
        np.nan,
    )
    actual_side = frame["move_first_hit_side"].astype(str).str.strip().str.lower().to_numpy(dtype=object)
    move_label = pd.to_numeric(frame["move_label"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    horizon_minutes = pd.to_numeric(frame["label_horizon_minutes"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    report_paths: Dict[float, str] = {}
    for threshold in direction_thresholds:
        move_ok = move_prob >= float(move_threshold)
        buy_up = move_ok & (direction_prob >= float(threshold))
        buy_down = move_ok & (direction_prob <= (1.0 - float(threshold)))
        chosen = buy_up | buy_down
        if not np.any(chosen):
            empty_path = reports_root / f"trades_move_{float(move_threshold):.2f}_dir_{float(threshold):.2f}.csv"
            pd.DataFrame(
                columns=[
                    "timestamp",
                    "trade_date",
                    "entry_ts",
                    "entry_price",
                    "horizon_end_ts",
                    "horizon_end_price",
                    "horizon_end_points",
                    "move_prob",
                    "direction_up_prob",
                    "move_threshold",
                    "direction_threshold",
                    "chosen_action",
                    "chosen_side",
                    "actual_first_hit_side",
                    "actual_move_label",
                    "prediction_correct",
                    "long_forward_return",
                    "short_forward_return",
                    "chosen_side_horizon_points",
                    "chosen_side_gross_return",
                    "chosen_side_net_return_after_cost",
                ]
            ).to_csv(empty_path, index=False)
            report_paths[float(threshold)] = str(empty_path.resolve())
            continue
        chosen_side = np.where(buy_up, "UP", np.where(buy_down, "DOWN", "HOLD"))
        chosen_points = np.where(buy_up, horizon_end_points, np.where(buy_down, -horizon_end_points, np.nan))
        gross_return = np.where(buy_up, long_ret, np.where(buy_down, short_ret, np.nan))
        net_return = np.where(np.isfinite(gross_return), gross_return - float(cost_per_trade), np.nan)
        prediction_correct = np.where(
            buy_up,
            actual_side == "up",
            np.where(buy_down, actual_side == "down", False),
        )
        report = pd.DataFrame(
            {
                "timestamp": timestamps,
                "trade_date": frame["trade_date"].astype(str),
                "entry_ts": timestamps + pd.to_timedelta(1, unit="min"),
                "entry_price": entry_price,
                "horizon_end_ts": timestamps + pd.to_timedelta(horizon_minutes, unit="min"),
                "horizon_end_price": horizon_end_price,
                "horizon_end_points": horizon_end_points,
                "move_prob": move_prob,
                "direction_up_prob": direction_prob,
                "move_threshold": float(move_threshold),
                "direction_threshold": float(threshold),
                "chosen_action": np.where(buy_up, "BUY_UP", np.where(buy_down, "BUY_DOWN", "HOLD")),
                "chosen_side": chosen_side,
                "actual_first_hit_side": actual_side,
                "actual_move_label": move_label,
                "prediction_correct": prediction_correct.astype(bool),
                "long_forward_return": long_ret,
                "short_forward_return": short_ret,
                "chosen_side_horizon_points": chosen_points,
                "chosen_side_gross_return": gross_return,
                "chosen_side_net_return_after_cost": net_return,
            }
        )
        report = report.loc[chosen].copy().reset_index(drop=True)
        path = reports_root / f"trades_move_{float(move_threshold):.2f}_dir_{float(threshold):.2f}.csv"
        report.to_csv(path, index=False)
        report_paths[float(threshold)] = str(path.resolve())
    return report_paths


def run_direction_from_move_quick(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    args = _build_parser().parse_args(argv)
    resolved = _resolve_config(args)
    run_dir = _prepare_run_dir(resolved)
    _validate_or_persist_config(run_dir, resolved)
    summary_path = run_dir / "summary.json"
    if bool(resolved["outputs"]["resume"]) and summary_path.exists():
        _append_state(run_dir, "job_resume_complete", summary_path=str(summary_path.resolve()))
        return _read_json(summary_path)
    _append_state(run_dir, "job_start", output_root=str(run_dir), resume=bool(resolved["outputs"]["resume"]))
    stage1_run_dir = Path(resolved["inputs"]["stage1_run_dir"]).resolve()
    _ensure_stage1_artifacts(stage1_run_dir)
    _write_json(run_dir / "stage1_reference.json", {"stage1_run_dir": str(stage1_run_dir)})

    model_window_labeled_path = run_dir / "model_window_labeled.parquet"
    holdout_labeled_path = run_dir / "holdout_labeled.parquet"
    if model_window_labeled_path.exists() and holdout_labeled_path.exists():
        model_window_labeled = pd.read_parquet(model_window_labeled_path)
        holdout_labeled = pd.read_parquet(holdout_labeled_path)
        _append_state(run_dir, "stage1_labeled_reused")
    else:
        _append_state(run_dir, "stage1_labeled_copy_start")
        model_window_labeled, holdout_labeled = _load_stage1_frames(stage1_run_dir)
        model_window_labeled.to_parquet(model_window_labeled_path, index=False)
        holdout_labeled.to_parquet(holdout_labeled_path, index=False)
        _append_state(run_dir, "stage1_labeled_copy_done", model_window_rows=int(len(model_window_labeled)), holdout_rows=int(len(holdout_labeled)))

    training_report_path = run_dir / "training_report.json"
    model_package_path = run_dir / "model.joblib"
    if training_report_path.exists() and model_package_path.exists():
        training_report = _read_json(training_report_path)
        model_package = dict(joblib.load(model_package_path))
        _append_state(run_dir, "training_reused", training_report=str(training_report_path), model_package=str(model_package_path))
    else:
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
            label_target="move_direction_up",
            model_whitelist=list(resolved["training"]["models"]),
            feature_set_whitelist=list(resolved["training"]["feature_sets"]),
            fit_all_final_models=False,
        )
        training_report = dict(training_result["report"])
        model_package = dict(training_result["model_package"])
        _write_json(training_report_path, training_report)
        joblib.dump(model_package, model_package_path)
        _append_state(run_dir, "training_done", training_report=str(training_report_path), model_package=str(model_package_path))

    direction_probs_path = run_dir / "holdout_direction_probabilities.parquet"
    direction_predictions_path = run_dir / "holdout_direction_predictions.csv"
    stage1_probs_path = run_dir / "holdout_move_probabilities.parquet"
    if direction_probs_path.exists() and direction_predictions_path.exists() and stage1_probs_path.exists():
        direction_probs = pd.read_parquet(direction_probs_path)
        stage1_probs = pd.read_parquet(stage1_probs_path)
        _append_state(run_dir, "holdout_scoring_reused")
    else:
        _append_state(run_dir, "holdout_scoring_start")
        direction_probs, _ = predict_probabilities_from_frame(holdout_labeled, model_package, context="direction_from_move_quick:holdout")
        stage1_probs = _score_stage1_holdout(stage1_run_dir, holdout_labeled)
        direction_probs.to_parquet(direction_probs_path, index=False)
        stage1_probs.to_parquet(stage1_probs_path, index=False)
        direction_predictions = holdout_labeled.loc[:, ["timestamp", "trade_date", "move_label", "move_first_hit_side", "long_forward_return", "short_forward_return"]].copy()
        direction_predictions["move_prob"] = pd.to_numeric(stage1_probs["move_prob"], errors="coerce")
        direction_predictions["direction_up_prob"] = pd.to_numeric(direction_probs["direction_up_prob"], errors="coerce")
        direction_predictions.to_csv(direction_predictions_path, index=False)
        _append_state(run_dir, "holdout_scoring_done", direction_predictions=str(direction_predictions_path))

    quality = _direction_quality(holdout_labeled, direction_probs, resolved["gating"]["direction_threshold_grid"])
    combined = _combined_holdout_summary(
        holdout_labeled,
        stage1_probs,
        direction_probs,
        move_threshold=float(resolved["gating"]["move_threshold"]),
        direction_thresholds=resolved["gating"]["direction_threshold_grid"],
        cost_per_trade=float(resolved["gating"]["cost_per_trade"]),
    )
    trade_report_paths = _write_combined_trade_reports(
        run_dir,
        holdout_labeled,
        stage1_probs,
        direction_probs,
        move_threshold=float(resolved["gating"]["move_threshold"]),
        direction_thresholds=resolved["gating"]["direction_threshold_grid"],
        cost_per_trade=float(resolved["gating"]["cost_per_trade"]),
    )
    for row in combined["thresholds"]:
        row["trade_report_path"] = trade_report_paths.get(float(row["direction_threshold"]))
    summary = {
        "status": "completed",
        "created_at_utc": _utc_now(),
        "paths": {
            "stage1_run_dir": str(stage1_run_dir),
            "model_window_labeled": str(model_window_labeled_path.resolve()),
            "holdout_labeled": str(holdout_labeled_path.resolve()),
            "training_report": str(training_report_path.resolve()),
            "model_package": str(model_package_path.resolve()),
            "holdout_direction_probabilities": str(direction_probs_path.resolve()),
            "holdout_move_probabilities": str(stage1_probs_path.resolve()),
            "holdout_direction_predictions": str(direction_predictions_path.resolve()),
            "combined_holdout_dir": str((run_dir / "combined_holdout").resolve()),
            "resolved_config": str((run_dir / "resolved_config.json").resolve()),
            "state": str((run_dir / "state.jsonl").resolve()),
        },
        "config": resolved,
        "training_report": training_report,
        "holdout_direction_quality": quality,
        "holdout_combined_summary": combined,
        "output_root": str(run_dir),
    }
    _write_json(summary_path, summary)
    _append_state(run_dir, "summary_done", summary_path=str(summary_path.resolve()))
    _append_state(run_dir, "job_done", status="completed")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    resolved = _resolve_config(args)
    if args.print_resolved_config:
        print(json.dumps(resolved, indent=2))
        return 0
    summary = run_direction_from_move_quick(argv)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
