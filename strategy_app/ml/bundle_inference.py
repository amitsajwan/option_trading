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


def _is_nan_value(v: Any) -> bool:
    """Check if a value is NaN (float NaN)."""
    return isinstance(v, float) and v != v


_VIX_HIGH_THRESHOLD = 18.0


def _compute_essential_features(flat: Dict[str, Any]) -> None:
    """Compute features not directly available in stage views but needed by retrained models.

    Mutates `flat` in-place, adding keys only if they're missing or NaN.
    """
    import math as _math

    def _safe_float(v: Any) -> Optional[float]:
        try:
            f = float(v)
            return f if _math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    # atm_iv = (atm_ce_iv + atm_pe_iv) / 2
    if not flat.get("atm_iv") or _is_nan_value(flat.get("atm_iv")):
        ce_iv = _safe_float(flat.get("atm_ce_iv"))
        pe_iv = _safe_float(flat.get("atm_pe_iv"))
        if ce_iv is not None and pe_iv is not None:
            flat["atm_iv"] = (ce_iv + pe_iv) / 2.0

    # iv_pct_rank_session: intraday pct rank of atm_iv — not computable from a single
    # snapshot. Use iv_percentile (historical) as proxy, same as pre-refactor behaviour.
    if not flat.get("iv_pct_rank_session") or _is_nan_value(flat.get("iv_pct_rank_session")):
        iv_pct = _safe_float(flat.get("iv_percentile"))
        if iv_pct is not None:
            flat["iv_pct_rank_session"] = iv_pct

    # vix_open_day: first VIX of the day — not available in stage views.
    # Use vix_prev_close as proxy (same as pre-refactor behaviour).
    if not flat.get("vix_open_day") or _is_nan_value(flat.get("vix_open_day")):
        vix_prev = _safe_float(flat.get("vix_prev_close"))
        if vix_prev is not None:
            flat["vix_open_day"] = vix_prev

    # is_high_vix_day: 1 if vix_open_day > threshold, else 0.
    if not flat.get("is_high_vix_day") or _is_nan_value(flat.get("is_high_vix_day")):
        vix_open = _safe_float(flat.get("vix_open_day"))
        if vix_open is not None:
            flat["is_high_vix_day"] = 1.0 if vix_open > _VIX_HIGH_THRESHOLD else 0.0

    # minute_of_day: absolute minutes since midnight (e.g. 555 for 9:15).
    # Stage views have minutes_since_open (0-based from 9:15). Convert by adding 555.
    if not flat.get("minute_of_day") or _is_nan_value(flat.get("minute_of_day")):
        mso = _safe_float(flat.get("minutes_since_open"))
        if mso is not None:
            flat["minute_of_day"] = mso + 555.0


def build_feature_row(snap: SnapshotAccessor, features: List[str]) -> Optional[Dict[str, float]]:
    snap_id = snap.snapshot_id or "unknown"
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
        # raw_payload scalars: use _fill so root-level values (e.g. compression features)
        # can overwrite None projected by views for keys the view spec doesn't resolve.
        _fill(flat, {k: v for k, v in snap.raw_payload.items() if not isinstance(v, (dict, list))})
        vel = snap.velocity_features
        if isinstance(vel, dict):
            # Velocity features (11:30-anchored) fill gaps; per-bar compression values win.
            _fill(flat, vel)

        # Compute essential features not available in stage views.
        _compute_essential_features(flat)

        row: Dict[str, float] = {}
        for feature in features:
            val = flat.get(feature)
            try:
                row[feature] = float(val) if val is not None else float("nan")
            except (TypeError, ValueError):
                row[feature] = float("nan")
        return row
    except Exception:
        logger.warning(
            "bundle_inference: feature extraction FAILED snap=%s — no vote possible",
            snap_id,
            exc_info=True,
        )
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

    snap_id = snap.snapshot_id or "unknown"
    features: List[str] = list(bundle.get("features") or [])
    if not features:
        logger.warning("bundle_inference: bundle has no features snap=%s", snap_id)
        return None
    row = build_feature_row(snap, features)
    if row is None:
        return None
    medians: Dict[str, float] = dict(bundle.get("feature_medians") or {})

    nan_features = [f for f, v in row.items() if not math.isfinite(v)]
    if nan_features:
        logger.warning(
            "bundle_inference: %d/%d features NaN snap=%s nan_features=%s",
            len(nan_features), len(features), snap_id, nan_features[:15],
        )
        if max_nan_features is not None and len(nan_features) > max_nan_features:
            logger.warning(
                "bundle_inference: %d NaN > max_nan_features=%d snap=%s — refusing inference (data not ready)",
                len(nan_features), max_nan_features, snap_id,
            )
            return None
    else:
        logger.debug("bundle_inference: all %d features finite snap=%s", len(features), snap_id)

    row_filled = {
        feature: (value if math.isfinite(value) else medians.get(feature, 0.0))
        for feature, value in row.items()
    }
    try:
        frame = pd.DataFrame([row_filled], columns=features)
        model = bundle["model"]
        prob = float(model.predict_proba(frame)[0, 1])
        if 0.0 <= prob <= 1.0:
            logger.debug(
                "bundle_inference: inference OK snap=%s prob=%.4f nan=%d/%d",
                snap_id, prob, len(nan_features), len(features),
            )
            return prob
        logger.warning(
            "bundle_inference: prob=%.4f out of [0,1] snap=%s — discarding",
            prob, snap_id,
        )
    except Exception:
        logger.warning(
            "bundle_inference: predict_proba FAILED snap=%s features=%d",
            snap_id, len(features),
            exc_info=True,
        )
    return None
