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
        if expected_kind == "entry_only_bundle":
            _check_xgboost_version(bundle)
        n_features = len(bundle.get("features") or [])
        logger.info("bundle_inference: loaded %s kind=%s features=%d", path, expected_kind, n_features)
        return bundle
    except Exception:
        logger.exception("bundle_inference: failed to load %s", path)
        return None


def build_feature_row(snap: SnapshotAccessor, features: List[str]) -> Optional[Dict[str, float]]:
    try:
        from snapshot_app.core.stage_views import project_stage_views_v2

        import math

        def _is_nan(v: Any) -> bool:
            return isinstance(v, float) and math.isnan(v)

        def _fill(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
            """Merge src into dst; never overwrite an existing non-NaN value with NaN."""
            for k, v in src.items():
                existing = dst.get(k)
                if existing is None or _is_nan(existing):
                    dst[k] = v

        views = project_stage_views_v2(snap.raw_payload)
        flat: Dict[str, Any] = {}
        for view_dict in views.values():
            if isinstance(view_dict, dict):
                _fill(flat, view_dict)
        for key, value in snap.raw_payload.items():
            if key not in flat and not isinstance(value, (dict, list)):
                flat[key] = value
        vel = snap.velocity_features
        if isinstance(vel, dict):
            # Velocity features (11:30-anchored) fill gaps; per-bar compression values win.
            _fill(flat, vel)
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


def _check_xgboost_version(bundle: Dict[str, Any]) -> None:
    """Warn if the xgboost version at serve time differs from what was available at train time.

    xgboost changed its predict_proba output format between 2.x and 3.x — a version
    mismatch silently produces garbage probabilities (e.g. all bars return 0.826).
    Root cause of the 2026-06-13 live incident where two identical prob=0.826 trades
    fired because the model was trained on 3.2.0 but served on 2.1.4.
    """
    try:
        import xgboost as xgb  # noqa: F401
        live_ver = getattr(xgb, "__version__", "unknown")
        # The bundle records the training xgboost version if published via ml_pipeline_2.
        train_ver = bundle.get("xgboost_version") or bundle.get("meta", {}).get("xgboost_version")
        if train_ver and train_ver != live_ver:
            logger.error(
                "INTEGRITY WARNING: model trained on xgboost %s but serving on %s. "
                "Probabilities may be corrupted — retrain or pin xgboost==%s in the serving image.",
                train_ver, live_ver, train_ver,
            )
        else:
            logger.info("bundle_inference: xgboost version at serve time: %s (train_ver=%s)", live_ver, train_ver or "not recorded")
    except ImportError:
        pass


def predict_positive_class_prob(
    bundle: Dict[str, Any],
    snap: SnapshotAccessor,
    max_nan_features: Optional[int] = None,
) -> Optional[float]:
    """Return P(positive class) or None.

    max_nan_features: if set and the number of NaN features at inference time
    exceeds this limit, return None instead of imputing medians.  Use this as a
    *data-readiness gate* — at market open or on stale snapshots many velocity
    features are missing; imputing medians produces garbage probs that the
    threshold cannot correct for.  Callers should set this to ~15 for 51-feature
    bundles (allows the known 8 structural NaNs, blocks 40+ missing-feature bars).
    """
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

    nan_features = [f for f, v in row.items() if not math.isfinite(v)]
    if nan_features:
        logger.warning(
            "bundle_inference: %d/%d features NaN: %s",
            len(nan_features), len(features), nan_features[:15],
        )
        if max_nan_features is not None and len(nan_features) > max_nan_features:
            # Data not ready — refuse to score rather than impute bad medians.
            # This fires at market open (velocity/delta features need history)
            # and on stale snapshot formats that lack vel_* fields entirely.
            logger.warning(
                "bundle_inference: %d NaN > max_nan_features=%d — refusing inference (data not ready)",
                len(nan_features), max_nan_features,
            )
            return None

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
