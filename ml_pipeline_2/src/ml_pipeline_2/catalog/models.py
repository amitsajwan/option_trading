from __future__ import annotations

from typing import Dict, List

from ..contracts.types import ModelSpec


DEFAULT_MODEL_SPECS: List[ModelSpec] = [
    ModelSpec(name="logreg_c1", family="logreg", params={"c": 1.0, "max_iter": 1000}),
    ModelSpec(name="logreg_balanced", family="logreg", params={"c": 0.5, "class_weight": "balanced", "max_iter": 1000}),
    ModelSpec(name="logreg_c01", family="logreg", params={"c": 0.1, "max_iter": 1000}),
    ModelSpec(name="logreg_c5", family="logreg", params={"c": 5.0, "max_iter": 1000}),
    ModelSpec(
        name="lgbm_fast",
        family="lgbm",
        params={"n_estimators": 220, "learning_rate": 0.05, "num_leaves": 31, "subsample": 0.9, "colsample_bytree": 0.9},
    ),
    ModelSpec(
        name="lgbm_dart",
        family="lgbm",
        params={
            "boosting_type": "dart",
            "n_estimators": 260,
            "learning_rate": 0.04,
            "num_leaves": 31,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "reg_lambda": 1.0,
        },
    ),
    ModelSpec(
        name="lgbm_large_v1",
        family="lgbm",
        params={
            "boosting_type": "gbdt",
            "num_leaves": 63,
            "max_depth": 8,
            "n_estimators": 500,
            "learning_rate": 0.03,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "reg_lambda": 2.0,
            "min_child_samples": 30,
        },
    ),
    ModelSpec(
        name="lgbm_large_dart_v1",
        family="lgbm",
        params={
            "boosting_type": "dart",
            "num_leaves": 63,
            "max_depth": 8,
            "n_estimators": 450,
            "learning_rate": 0.03,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "reg_lambda": 2.0,
            "min_child_samples": 30,
        },
    ),
    ModelSpec(name="xgb_fast", family="xgb", params={"max_depth": 3, "n_estimators": 220, "learning_rate": 0.05, "subsample": 0.9, "colsample_bytree": 0.9}),
    ModelSpec(
        name="xgb_balanced",
        family="xgb",
        params={
            "max_depth": 4,
            "n_estimators": 350,
            "learning_rate": 0.03,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "reg_lambda": 2.0,
        },
    ),
    ModelSpec(
        name="xgb_regularized",
        family="xgb",
        params={
            "max_depth": 4,
            "n_estimators": 450,
            "learning_rate": 0.02,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 1.0,
            "reg_lambda": 4.0,
        },
    ),
    ModelSpec(
        name="xgb_deep_v1",
        family="xgb",
        params={
            "max_depth": 6,
            "n_estimators": 500,
            "learning_rate": 0.03,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "reg_alpha": 0.5,
            "reg_lambda": 4.0,
        },
    ),
    ModelSpec(
        name="xgb_deep_slow_v1",
        family="xgb",
        params={
            "max_depth": 6,
            "n_estimators": 800,
            "learning_rate": 0.015,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 1.0,
            "reg_lambda": 5.0,
        },
    ),
    ModelSpec(
        name="xgb_shallow",
        family="xgb",
        params={
            "max_depth": 2,
            "n_estimators": 500,
            "learning_rate": 0.025,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "reg_lambda": 2.0,
        },
    ),
]


def model_specs_by_name() -> Dict[str, ModelSpec]:
    return {spec.name: spec for spec in DEFAULT_MODEL_SPECS}


def model_names() -> List[str]:
    return sorted(model_specs_by_name())
