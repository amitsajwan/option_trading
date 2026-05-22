"""Shared feature extraction and sklearn inference for ML model bundles."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from ..market.snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)


def load_joblib_bundle(path: str, *, expected_kind: str) -> Optional[Dict[str, Any]]:
    try:
        import joblib

        bundle = joblib.load(path)
        if not isinstance(bundle, dict) or bundle.get("kind") != expected_kind:
            logger.warning(
                "bundle_inference: expected kind=%s at %s got %s",
                expected_kind,
                path,
                bundle.get("kind") if isinstance(bundle, dict) else type(bundle),
            )
            return None
        return bundle
    except Exception:
        logger.exception("bundle_inference: failed to load %s", path)
        return None


def build_feature_row(snap: SnapshotAccessor, features: List[str]) -> Optional[Dict[str, float]]:
    try:
        from snapshot_app.core.stage_views import project_stage_views_v2

        views = project_stage_views_v2(snap.raw_payload)
        flat: Dict[str, Any] = {}
        for view_dict in views.values():
            if isinstance(view_dict, dict):
                flat.update(view_dict)
        for key, value in snap.raw_payload.items():
            if key not in flat and not isinstance(value, (dict, list)):
                flat[key] = value
        vel = snap.velocity_features
        if isinstance(vel, dict):
            flat.update(vel)
        row: Dict[str, float] = {}
        for feature in features:
            val = flat.get(feature)
            try:
                row[feature] = float(val) if val is not None else float("nan")
            except (TypeError, ValueError):
                row[feature] = float("nan")
        return row
    except Exception:
        logger.debug("bundle_inference: feature extraction failed", exc_info=True)
        return None


def predict_positive_class_prob(bundle: Dict[str, Any], snap: SnapshotAccessor) -> Optional[float]:
    try:
        import pandas as pd
    except ImportError:
        return None

    features: List[str] = list(bundle.get("features") or [])
    if not features:
        return None
    row = build_feature_row(snap, features)
    if row is None:
        return None
    medians: Dict[str, float] = dict(bundle.get("feature_medians") or {})
    row_filled = {
        feature: (value if math.isfinite(value) else medians.get(feature, 0.0))
        for feature, value in row.items()
    }
    try:
        frame = pd.DataFrame([row_filled], columns=features)
        model = bundle["model"]
        prob = float(model.predict_proba(frame)[0, 1])
        if 0.0 <= prob <= 1.0:
            return prob
    except Exception:
        logger.debug("bundle_inference: predict failed", exc_info=True)
    return None
