from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.preprocessing import StandardScaler

try:
    from xgboost import XGBClassifier
except Exception:  # pragma: no cover - optional dependency in some envs
    XGBClassifier = None  # type: ignore[assignment]

from .entry_quality_config import (
    DEFAULT_CANDIDATE_ROOT,
    DEFAULT_MODEL_ROOT,
    FEATURE_PROFILES,
    LABEL_PROFILES,
    MODEL_SPECS,
    SEGMENTATION_POLICIES,
    THRESHOLD_POLICIES,
    candidate_feature_columns_for_profile,
    normalize_feature_profile_ids,
)
from snapshot_app.historical.window_manifest import (
    DEFAULT_MIN_TRADING_DAYS,
    DEFAULT_REQUIRED_SCHEMA_VERSION,
    load_and_validate_window_manifest,
)
from .modeling.calibration import (
    CALIBRATION_ISOTONIC,
    CALIBRATION_NONE,
    CALIBRATION_PLATT,
)
from .safe_feature_names import SafeFeatureNameTransformer


CAL_METHODS = (CALIBRATION_NONE, CALIBRATION_PLATT, CALIBRATION_ISOTONIC)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_list(values: Optional[str], *, default: list[str]) -> list[str]:
    if not values:
        return list(default)
    return [item.strip() for item in str(values).split(",") if item.strip()]


def _metrics(y_true: pd.Series, prob: pd.Series | list[float] | Any) -> dict[str, float | int | None]:
    y = pd.Series(y_true).astype(int)
    p = pd.Series(prob, index=y.index).astype(float)
    out: dict[str, float | int | None] = {
        "rows": int(len(y)),
        "positive_rate": float(y.mean()) if len(y) else None,
        "brier": float(brier_score_loss(y, p)) if len(y) else None,
    }
    if len(y.unique()) >= 2:
        out["roc_auc"] = float(roc_auc_score(y, p))
        out["pr_auc"] = float(average_precision_score(y, p))
    else:
        out["roc_auc"] = None
        out["pr_auc"] = None
    return out


def _fit_calibrator(method: str, valid_raw: pd.Series, y_valid: pd.Series) -> object | None:
    mode = str(method or CALIBRATION_NONE).strip().lower()
    if mode == CALIBRATION_NONE:
        return None
    if mode == CALIBRATION_PLATT:
        lr = LogisticRegression(max_iter=200, solver="lbfgs")
        lr.fit(valid_raw.to_numpy().reshape(-1, 1), y_valid.astype(int).to_numpy())
        return lr
    if mode == CALIBRATION_ISOTONIC:
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(valid_raw.to_numpy(), y_valid.astype(int).to_numpy())
        return iso
    raise ValueError(f"unsupported calibration method: {method}")


def _apply_calibrator(method: str, calibrator: object | None, scores: pd.Series) -> pd.Series:
    mode = str(method or CALIBRATION_NONE).strip().lower()
    if calibrator is None or mode == CALIBRATION_NONE:
        return scores.astype(float)
    if mode == CALIBRATION_PLATT:
        values = calibrator.predict_proba(scores.to_numpy().reshape(-1, 1))[:, 1]
        return pd.Series(values, index=scores.index, dtype=float)
    if mode == CALIBRATION_ISOTONIC:
        values = calibrator.predict(scores.to_numpy())
        return pd.Series(values, index=scores.index, dtype=float)
    return scores.astype(float)


def _threshold_sweep(scores: pd.Series, target: pd.Series, thresholds: list[float]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for thr in thresholds:
        mask = scores >= thr
        selected = target[mask]
        rows.append(
            {
                "threshold": float(thr),
                "rows": int(mask.sum()),
                "positive_rate": (float(selected.mean()) if len(selected) else None),
            }
        )
    return pd.DataFrame(rows)


def _select_threshold(sweep: pd.DataFrame, total_rows: int) -> float:
    if sweep.empty:
        return 0.60
    minimum_rows = max(10, int(total_rows * 0.05))
    eligible = sweep[sweep["rows"] >= minimum_rows].copy()
    if eligible.empty:
        eligible = sweep.copy()
    eligible = eligible.sort_values(["positive_rate", "rows", "threshold"], ascending=[False, False, True], kind="stable")
    return float(eligible.iloc[0]["threshold"])


def _build_estimator(model_family: str, params: dict[str, Any], *, random_state: int = 42) -> object:
    family = str(model_family).strip().lower()
    if family == "logreg":
        return LogisticRegression(random_state=random_state, **params)
    if family == "lgbm":
        return LGBMClassifier(random_state=random_state, n_jobs=1, **params)
    if family == "xgb":
        if XGBClassifier is None:
            raise RuntimeError("xgboost not available")
        xgb_params = dict(params)
        xgb_params.setdefault("n_estimators", 200)
        xgb_params.setdefault("eval_metric", "logloss")
        xgb_params.setdefault("tree_method", "hist")
        return XGBClassifier(
            random_state=random_state,
            n_jobs=1,
            **xgb_params,
        )
    raise ValueError(f"unsupported model family: {model_family}")


def _build_pipeline(
    *,
    model_family: str,
    params: dict[str, Any],
    numeric_columns: list[str],
    categorical_columns: list[str],
) -> Pipeline:
    family = str(model_family).strip().lower()
    numeric_steps: list[tuple[str, Any]] = [("imputer", SimpleImputer(strategy="median"))]
    if family == "logreg":
        # Logistic regression is sensitive to feature scale; scaling improves convergence
        # without affecting the tree-based models used elsewhere.
        numeric_steps.append(("scaler", StandardScaler()))

    preprocess = ColumnTransformer(
        transformers=[
            (
                "num",
                Pipeline(numeric_steps),
                numeric_columns,
            ),
            (
                "cat",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="constant", fill_value="__MISSING__")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                    ]
                ),
                categorical_columns,
            ),
        ]
    )
    try:
        # Preserve generated feature names through the preprocessor so downstream
        # estimators see the same named columns at fit and predict time.
        preprocess.set_output(transform="pandas")
    except AttributeError:
        pass
    estimator = _build_estimator(model_family, params)
    return Pipeline(
        [
            ("preprocess", preprocess),
            ("safe_names", SafeFeatureNameTransformer()),
            ("model", estimator),
        ]
    )


def _feature_importance(pipeline: Pipeline) -> pd.DataFrame:
    pre = pipeline.named_steps["preprocess"]
    model = pipeline.named_steps["model"]
    feature_names = list(pre.get_feature_names_out())
    if hasattr(model, "feature_importances_"):
        values = getattr(model, "feature_importances_")
        return pd.DataFrame({"feature": feature_names, "importance": values}).sort_values(
            "importance", ascending=False, kind="stable"
        )
    if hasattr(model, "coef_"):
        coef = np.ravel(getattr(model, "coef_"))
        return pd.DataFrame({"feature": feature_names, "importance": np.abs(coef)}).sort_values(
            "importance", ascending=False, kind="stable"
        )
    return pd.DataFrame({"feature": feature_names, "importance": np.nan})


def _segment_frames(frame: pd.DataFrame, policy_id: str) -> dict[str, pd.DataFrame]:
    policy = SEGMENTATION_POLICIES[policy_id]
    if policy.use_global_model:
        return {"GLOBAL": frame.copy()}
    out: dict[str, pd.DataFrame] = {}
    for segment in policy.segments:
        seg_df = frame[frame["regime"] == segment].copy()
        if not seg_df.empty:
            out[segment] = seg_df
    return out


def _split_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    train = frame[frame["split_name"] == "train"].copy()
    valid = frame[frame["split_name"] == "valid"].copy()
    eval_df = frame[frame["split_name"] == "eval"].copy()
    return train, valid, eval_df


def _aggregate_segment_metrics(segment_summaries: list[dict[str, Any]]) -> dict[str, float | int | None]:
    if not segment_summaries:
        return {
            "offline_valid_roc_auc": None,
            "offline_valid_pr_auc": None,
            "offline_valid_brier": None,
            "offline_eval_roc_auc": None,
            "offline_eval_pr_auc": None,
            "offline_eval_brier": None,
            "offline_train_rows": 0,
            "offline_valid_rows": 0,
            "offline_eval_rows": 0,
            "offline_rank_score": None,
        }

    def _weighted(metric_key: str, row_key: str, metric_group: str) -> float | None:
        total = 0
        weighted = 0.0
        for segment in segment_summaries:
            rows = int(segment.get(row_key, 0) or 0)
            value = (segment.get(metric_group) or {}).get(metric_key)
            if rows <= 0 or value is None:
                continue
            total += rows
            weighted += float(value) * rows
        if total <= 0:
            return None
        return weighted / total

    valid_roc = _weighted("roc_auc", "valid_rows", "selected_valid_metrics")
    valid_pr = _weighted("pr_auc", "valid_rows", "selected_valid_metrics")
    valid_brier = _weighted("brier", "valid_rows", "selected_valid_metrics")
    eval_roc = _weighted("roc_auc", "eval_rows", "selected_eval_metrics")
    eval_pr = _weighted("pr_auc", "eval_rows", "selected_eval_metrics")
    eval_brier = _weighted("brier", "eval_rows", "selected_eval_metrics")

    rank_parts = [item for item in [eval_pr, eval_roc] if item is not None]
    rank_score = float(sum(rank_parts) / len(rank_parts)) if rank_parts else None
    if rank_score is not None and eval_brier is not None:
        rank_score -= float(eval_brier)

    return {
        "offline_valid_roc_auc": valid_roc,
        "offline_valid_pr_auc": valid_pr,
        "offline_valid_brier": valid_brier,
        "offline_eval_roc_auc": eval_roc,
        "offline_eval_pr_auc": eval_pr,
        "offline_eval_brier": eval_brier,
        "offline_train_rows": int(sum(int(item.get("train_rows", 0) or 0) for item in segment_summaries)),
        "offline_valid_rows": int(sum(int(item.get("valid_rows", 0) or 0) for item in segment_summaries)),
        "offline_eval_rows": int(sum(int(item.get("eval_rows", 0) or 0) for item in segment_summaries)),
        "offline_rank_score": rank_score,
    }


def run_experiments(
    *,
    dataset_path: Path,
    output_root: Path,
    feature_profiles: list[str],
    label_profiles: list[str],
    segmentation_policies: list[str],
    model_families: list[str],
    threshold_policies: list[str],
    run_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(dataset_path).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    feature_profiles = normalize_feature_profile_ids(feature_profiles)

    registry_rows: list[dict[str, Any]] = []
    trained_experiments: list[dict[str, Any]] = []

    for feature_profile_id in feature_profiles:
        feature_profile = FEATURE_PROFILES[feature_profile_id]
        feature_columns = candidate_feature_columns_for_profile(feature_profile_id)
        numeric_columns = [col for col in feature_profile.numeric_columns if col in df.columns]
        categorical_columns = [col for col in feature_profile.categorical_columns if col in df.columns]

        for label_profile_id in label_profiles:
            label_profile = LABEL_PROFILES[label_profile_id]
            if label_profile.column_name not in df.columns:
                registry_rows.append(
                    {
                        "status": "skipped",
                        "reason": "label_column_missing",
                        "feature_profile_id": feature_profile_id,
                        "label_profile_id": label_profile_id,
                    }
                )
                continue
            labeled = df[df[label_profile.column_name].notna()].copy()
            if labeled.empty:
                registry_rows.append(
                    {
                        "status": "skipped",
                        "reason": "label_all_null",
                        "feature_profile_id": feature_profile_id,
                        "label_profile_id": label_profile_id,
                    }
                )
                continue
            labeled["target"] = labeled[label_profile.column_name].astype(int)

            for segmentation_id in segmentation_policies:
                segments = _segment_frames(labeled, segmentation_id)
                if not segments:
                    registry_rows.append(
                        {
                            "status": "skipped",
                            "reason": "no_segments",
                            "feature_profile_id": feature_profile_id,
                            "label_profile_id": label_profile_id,
                            "segmentation_policy_id": segmentation_id,
                        }
                    )
                    continue

                for model_id in model_families:
                    model_spec = MODEL_SPECS[model_id]
                    experiment_key = "__".join([feature_profile_id, label_profile_id, segmentation_id, model_id])
                    experiment_dir = output_root / experiment_key
                    experiment_dir.mkdir(parents=True, exist_ok=True)

                    segment_summaries: list[dict[str, Any]] = []
                    threshold_rows: list[pd.DataFrame] = []
                    segments_package: dict[str, Any] = {}
                    failed = False
                    fail_reason = None
                    for segment_name, frame in segments.items():
                        train, valid, eval_df = _split_frame(frame)
                        if train.empty or valid.empty or eval_df.empty:
                            failed = True
                            fail_reason = f"{segment_name}:insufficient_split_rows"
                            break
                        y_train = train["target"].astype(int)
                        y_valid = valid["target"].astype(int)
                        y_eval = eval_df["target"].astype(int)
                        if len(y_train.unique()) < 2 or len(y_valid.unique()) < 2 or len(y_eval.unique()) < 2:
                            failed = True
                            fail_reason = f"{segment_name}:single_class_split"
                            break

                        train_medians = {
                            column: float(pd.to_numeric(train[column], errors="coerce").median())
                            for column in numeric_columns
                            if column in train.columns
                        }

                        model_pipeline = _build_pipeline(
                            model_family=model_spec.family,
                            params=model_spec.params,
                            numeric_columns=numeric_columns,
                            categorical_columns=categorical_columns,
                        )
                        try:
                            model_pipeline.fit(train[feature_columns], y_train)
                        except Exception as exc:
                            failed = True
                            fail_reason = f"{segment_name}:fit_failed:{exc}"
                            break

                        valid_raw = pd.Series(model_pipeline.predict_proba(valid[feature_columns])[:, 1], index=valid.index, dtype=float)
                        eval_raw = pd.Series(model_pipeline.predict_proba(eval_df[feature_columns])[:, 1], index=eval_df.index, dtype=float)

                        best_method = CALIBRATION_NONE
                        best_brier = float("inf")
                        method_reports: list[dict[str, Any]] = []
                        for method in CAL_METHODS:
                            calibrator = _fit_calibrator(method, valid_raw, y_valid)
                            valid_score = _apply_calibrator(method, calibrator, valid_raw)
                            eval_score = _apply_calibrator(method, calibrator, eval_raw)
                            valid_metrics = _metrics(y_valid, valid_score)
                            eval_metrics = _metrics(y_eval, eval_score)
                            method_reports.append(
                                {
                                    "method": method,
                                    "valid_metrics": valid_metrics,
                                    "eval_metrics": eval_metrics,
                                }
                            )
                            brier = valid_metrics["brier"]
                            if brier is not None and brier < best_brier:
                                best_brier = float(brier)
                                best_method = method

                        calibrator = _fit_calibrator(best_method, valid_raw, y_valid)
                        valid_score = _apply_calibrator(best_method, calibrator, valid_raw)
                        eval_score = _apply_calibrator(best_method, calibrator, eval_raw)
                        sweep = _threshold_sweep(valid_score, y_valid, [x / 100 for x in range(30, 81, 5)])
                        selected_threshold = _select_threshold(sweep, len(valid))
                        sweep.insert(0, "segment_name", segment_name)
                        threshold_rows.append(sweep)
                        feature_importance = _feature_importance(model_pipeline)
                        feature_importance.to_csv(experiment_dir / f"{segment_name.lower()}_feature_importance.csv", index=False)
                        segments_package[segment_name] = {
                            "segment_name": segment_name,
                            "model": model_pipeline,
                            "feature_columns": feature_columns,
                            "numeric_columns": numeric_columns,
                            "categorical_columns": categorical_columns,
                            "numeric_fill_values": train_medians,
                            "calibration_method": best_method,
                            "calibrator": calibrator,
                            "threshold": float(selected_threshold),
                        }
                        segment_summaries.append(
                            {
                                "segment_name": segment_name,
                                "train_rows": int(len(train)),
                                "valid_rows": int(len(valid)),
                                "eval_rows": int(len(eval_df)),
                                "selected_calibration_method": best_method,
                                "selected_threshold": float(selected_threshold),
                                "selected_valid_metrics": _metrics(y_valid, valid_score),
                                "selected_eval_metrics": _metrics(y_eval, eval_score),
                                "calibration_methods": method_reports,
                            }
                        )

                    if failed:
                        registry_rows.append(
                            {
                                "status": "failed",
                                "reason": fail_reason,
                                "feature_profile_id": feature_profile_id,
                                "label_profile_id": label_profile_id,
                                "segmentation_policy_id": segmentation_id,
                                "model_id": model_id,
                            }
                        )
                        continue

                    bundle = {
                        "package_type": "entry_quality_segmented_v2",
                        "created_at_utc": _utc_now(),
                        "feature_profile_id": feature_profile_id,
                        "label_profile_id": label_profile_id,
                        "segmentation_policy_id": segmentation_id,
                        "model_id": model_id,
                        "segments": segments_package,
                    }
                    bundle_path = experiment_dir / "entry_quality_segmented_bundle.joblib"
                    joblib.dump(bundle, bundle_path)
                    summary_path = experiment_dir / "summary.json"
                    summary_path.write_text(json.dumps(segment_summaries, indent=2), encoding="utf-8")
                    combined_sweep = pd.concat(threshold_rows, ignore_index=True)
                    combined_sweep.to_csv(experiment_dir / "combined_threshold_sweep.csv", index=False)
                    aggregate_metrics = _aggregate_segment_metrics(segment_summaries)

                    trained_experiments.append(
                        {
                            "experiment_key": experiment_key,
                            "bundle_path": str(bundle_path).replace("\\", "/"),
                            "summary_path": str(summary_path).replace("\\", "/"),
                            "feature_profile_id": feature_profile_id,
                            "label_profile_id": label_profile_id,
                            "segmentation_policy_id": segmentation_id,
                            "model_id": model_id,
                            "segment_summaries": segment_summaries,
                        }
                    )

                    for threshold_policy_id in threshold_policies:
                        registry_rows.append(
                            {
                                "status": "trained",
                                "experiment_id": f"{experiment_key}__{threshold_policy_id}",
                                "base_experiment_key": experiment_key,
                                "bundle_path": str(bundle_path).replace("\\", "/"),
                                "feature_profile_id": feature_profile_id,
                                "label_profile_id": label_profile_id,
                                "segmentation_policy_id": segmentation_id,
                                "model_id": model_id,
                                "threshold_policy_id": threshold_policy_id,
                                "summary_path": str(summary_path).replace("\\", "/"),
                                **aggregate_metrics,
                            }
                        )

    registry_df = pd.DataFrame(registry_rows)
    registry_path = output_root / "experiment_registry.csv"
    registry_df.to_csv(registry_path, index=False)
    registry_json_path = output_root / "experiment_registry.json"
    registry_json_path.write_text(json.dumps(registry_rows, indent=2), encoding="utf-8")
    summary = {
        "created_at_utc": _utc_now(),
        "dataset_path": str(dataset_path).replace("\\", "/"),
        "feature_profiles": feature_profiles,
        "label_profiles": label_profiles,
        "segmentation_policies": segmentation_policies,
        "model_families": model_families,
        "threshold_policies": threshold_policies,
        "trained_variant_count": int((registry_df["status"] == "trained").sum()) if not registry_df.empty else 0,
        "output_root": str(output_root).replace("\\", "/"),
        "registry_csv": str(registry_path).replace("\\", "/"),
        "registry_json": str(registry_json_path).replace("\\", "/"),
    }
    (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if run_meta is not None:
        (output_root / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run canonical entry-quality model experiments.")
    parser.add_argument("--dataset", default=str(DEFAULT_CANDIDATE_ROOT / "entry_candidate_labels.parquet"))
    parser.add_argument("--output-dir", default=str(DEFAULT_MODEL_ROOT / "entry_quality_experiments"))
    parser.add_argument("--feature-profiles", default="eq_core_snapshot_v1,eq_full_v1")
    parser.add_argument("--label-profiles", default="mfe15_gt_5_v1")
    parser.add_argument("--segmentation-policies", default="seg_regime_v1")
    parser.add_argument("--model-families", default="logreg_baseline_v1,lgbm_default_v1,lgbm_regularized_v1")
    parser.add_argument("--threshold-policies", default="fixed_060,segment_optimal,strategy_override_v1")
    parser.add_argument("--window-manifest", default=None, help="Path to canonical window manifest JSON.")
    parser.add_argument("--formal-run", action="store_true", help="Enforce formal readiness rules from window manifest.")
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.formal_run and not args.window_manifest:
        raise SystemExit("--formal-run requires --window-manifest")
    manifest_meta: Optional[dict[str, Any]] = None
    if args.window_manifest:
        manifest_meta = load_and_validate_window_manifest(
            args.window_manifest,
            formal_run=bool(args.formal_run),
            required_schema_version=str(args.manifest_required_schema_version),
            min_trading_days=int(args.manifest_min_trading_days),
            context="entry_quality_experiments.window_manifest",
        )

    dataset_path = Path(args.dataset)
    split_bounds: dict[str, Any] = {}
    try:
        df = pd.read_parquet(dataset_path, columns=["trade_date", "split_name"])
        if not df.empty and "split_name" in df.columns:
            for split_name in ("train", "valid", "eval"):
                split_rows = df[df["split_name"] == split_name]
                if split_rows.empty:
                    continue
                values = split_rows["trade_date"].astype(str).sort_values(kind="stable")
                split_bounds[split_name] = {"start": str(values.iloc[0]), "end": str(values.iloc[-1]), "days": int(values.nunique())}
    except Exception:
        split_bounds = {}

    run_meta = {
        "generated_at_utc": _utc_now(),
        "command": "strategy_app.offline_ml.entry_quality_experiments",
        "formal_run": bool(args.formal_run),
        "exploratory_only": bool((manifest_meta or {}).get("exploratory_only", not bool(args.formal_run))),
        "window_manifest": manifest_meta,
        "manifest_path": (manifest_meta or {}).get("manifest_path"),
        "manifest_hash": (manifest_meta or {}).get("manifest_hash"),
        "window_start": (manifest_meta or {}).get("window_start"),
        "window_end": (manifest_meta or {}).get("window_end"),
        "split_boundaries": split_bounds,
        "gate_results": {
            "formal_ready": (manifest_meta or {}).get("formal_ready"),
            "required_schema_version": str(args.manifest_required_schema_version),
            "min_trading_days_required": int(args.manifest_min_trading_days),
            "window_trading_days": (manifest_meta or {}).get("trading_days"),
            "all_days_required_schema": (manifest_meta or {}).get("all_days_required_schema"),
        },
    }

    summary = run_experiments(
        dataset_path=dataset_path,
        output_root=Path(args.output_dir),
        feature_profiles=_ensure_list(args.feature_profiles, default=["eq_core_snapshot_v1"]),
        label_profiles=_ensure_list(args.label_profiles, default=["mfe15_gt_5_v1"]),
        segmentation_policies=_ensure_list(args.segmentation_policies, default=["seg_regime_v1"]),
        model_families=_ensure_list(args.model_families, default=["lgbm_default_v1"]),
        threshold_policies=_ensure_list(args.threshold_policies, default=["segment_optimal"]),
        run_meta=run_meta,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
