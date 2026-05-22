"""Direction-ML conflict resolver and optional filter for the deterministic engine.

Wraps any base EntryPolicy and adds a trained direction (CE vs PE) model as
an optional layer:

  Mode 1 — Conflict resolver (default, always active when model is loaded):
    When both CE and PE votes are present the base engine is stuck. This policy
    implements can_resolve_direction_conflict() so the engine scores each
    candidate and picks the one whose ML direction probability is higher.
    Blends base.score with ML direction probability so quality gates are
    still respected.

  Mode 2 — Direction filter (opt-in via DIRECTION_ML_FILTER_MIN_PROB env var):
    Even when rules agree on direction, the ML probability must exceed
    DIRECTION_ML_FILTER_MIN_PROB (0–1). Below that, entry is blocked.
    Useful once the model has proven itself in live shadow mode first.

Load the model bundle (produced by ml_pipeline_2/scripts/train_direction_only.py):
    export DIRECTION_ML_MODEL_PATH=/path/to/direction_only_model.joblib

Then the DeterministicRuleEngine will automatically wrap the active policy
with this resolver if the env var is set.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

import numpy as np

from ..contracts import Direction, RiskContext, StrategyVote
from .entry_policy import EntryPolicy, EntryPolicyDecision, PolicyConfig
from .regime import RegimeSignal
from .snapshot_accessor import SnapshotAccessor

logger = logging.getLogger(__name__)

# Weight of the ML direction score in the final blended score.
# 0.0 = ignore ML (pure base score); 1.0 = ignore base score.
_ML_WEIGHT_DEFAULT = 0.40

# If ML score can't be computed (e.g. velocity missing), fall back to this.
_ML_FALLBACK_SCORE = 0.50


def _load_bundle(path: str) -> Optional[Dict[str, Any]]:
    """Load a direction_only_model.joblib bundle. Returns None on failure."""
    try:
        import joblib
        bundle = joblib.load(path)
        if not isinstance(bundle, dict) or bundle.get("kind") != "direction_only_bundle":
            logger.warning("direction_ml_policy: unexpected bundle kind at %s — skipping", path)
            return None
        logger.info(
            "direction_ml_policy: loaded model from %s  features=%d  holdout_auc=%s",
            path,
            len(bundle.get("features", [])),
            bundle.get("holdout_eval", {}).get("roc_auc", "?"),
        )
        return bundle
    except Exception:
        logger.exception("direction_ml_policy: failed to load bundle from %s", path)
        return None


def _build_feature_row(snap: SnapshotAccessor, features: list[str]) -> Optional[Dict[str, float]]:
    """
    Build a flat feature dict from the live snapshot, matching the training feature names.
    Uses project_stage_views_v2 to extract the same flat format used during training.
    Returns None if extraction fails (ML will be skipped).
    """
    try:
        from snapshot_app.core.stage_views import project_stage_views_v2
        views = project_stage_views_v2(snap.raw_payload)
        # Merge all view dicts into one flat dict
        flat: Dict[str, Any] = {}
        for view_dict in views.values():
            if isinstance(view_dict, dict):
                flat.update(view_dict)
        # Also merge the raw payload top-level for any direct fields
        for k, v in snap.raw_payload.items():
            if k not in flat and not isinstance(v, (dict, list)):
                flat[k] = v
        # Also bring in velocity_enrichment directly (in case view didn't include all)
        vel = snap.velocity_features
        if isinstance(vel, dict):
            flat.update(vel)
        row = {}
        for f in features:
            val = flat.get(f)
            try:
                fval = float(val) if val is not None else float("nan")
            except (TypeError, ValueError):
                fval = float("nan")
            row[f] = fval
        return row
    except Exception:
        logger.debug("direction_ml_policy: feature extraction failed", exc_info=True)
        return None


def _predict_ce_prob(bundle: Dict[str, Any], snap: SnapshotAccessor) -> Optional[float]:
    """
    Return probability that this snapshot favours a CE (up) trade.
    Returns None if features can't be extracted (caller should fall back).
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    features: list[str] = bundle.get("features", [])
    if not features:
        return None

    row = _build_feature_row(snap, features)
    if row is None:
        return None

    medians: Dict[str, float] = bundle.get("feature_medians", {})
    row_filled = {f: (v if math.isfinite(v) else medians.get(f, 0.0)) for f, v in row.items()}

    try:
        df = pd.DataFrame([row_filled], columns=features)
        model = bundle["model"]
        prob = float(model.predict_proba(df)[0, 1])
        return prob if 0.0 <= prob <= 1.0 else None
    except Exception:
        logger.debug("direction_ml_policy: predict failed", exc_info=True)
        return None


class DirectionMLConflictResolver:
    """
    Wraps any base EntryPolicy and adds ML-based direction scoring.

    Drop-in replacement for LongOptionEntryPolicy / VelocityEnhancedEntryPolicy
    when DIRECTION_ML_MODEL_PATH is set.
    """

    def __init__(
        self,
        base_policy: EntryPolicy,
        bundle: Dict[str, Any],
        *,
        ml_weight: float = _ML_WEIGHT_DEFAULT,
        filter_min_prob: Optional[float] = None,
    ) -> None:
        self._base = base_policy
        self._bundle = bundle
        self._ml_weight = float(max(0.0, min(1.0, ml_weight)))
        self._filter_min_prob = filter_min_prob

    # ── properties forwarded to base policy ──────────────────────────────────

    @property
    def config(self) -> PolicyConfig:
        cfg = getattr(self._base, "config", None)
        return cfg if cfg is not None else PolicyConfig()

    # ── ML hooks used by DeterministicRuleEngine ──────────────────────────────

    def can_resolve_direction_conflict(self) -> bool:
        """Signal to the engine that we can break a CE-vs-PE tie using ML."""
        return True

    # ── primary evaluation ────────────────────────────────────────────────────

    def evaluate(
        self,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        base_decision = self._base.evaluate(snap, vote, regime, risk)

        # Compute ML direction probability for the requested direction
        ce_prob = _predict_ce_prob(self._bundle, snap)
        if ce_prob is None:
            ml_dir_score = _ML_FALLBACK_SCORE
            ml_status = "unavailable"
        else:
            if vote.direction == Direction.CE:
                ml_dir_score = ce_prob
            elif vote.direction == Direction.PE:
                ml_dir_score = 1.0 - ce_prob
            else:
                ml_dir_score = _ML_FALLBACK_SCORE
            ml_status = f"{'CE' if ce_prob >= 0.5 else 'PE'}:{ce_prob:.3f}"

        # Mode 2 — direction filter: block if ML is below minimum
        if self._filter_min_prob is not None and ce_prob is not None:
            if ml_dir_score < self._filter_min_prob:
                checks = {**(base_decision.checks if base_decision else {}),
                          "ml_direction": ml_status,
                          "ml_filter": f"blocked: {ml_dir_score:.3f} < {self._filter_min_prob}"}
                return EntryPolicyDecision.block(
                    f"ml_direction_filter: {ml_status}",
                    checks,
                )

        # If base policy blocks, respect it (ML does not override quality gates)
        if not base_decision.allowed:
            return base_decision

        # Blend base score with ML direction score
        base_score = float(base_decision.score)
        blended_score = (1.0 - self._ml_weight) * base_score + self._ml_weight * ml_dir_score

        checks = {**base_decision.checks,
                  "ml_direction": ml_status,
                  "ml_weight": f"{self._ml_weight:.2f}"}

        return EntryPolicyDecision.allow(
            f"ml_direction: {base_decision.reason}",
            score=blended_score,
            checks=checks,
            adjustments=base_decision.adjustments,
        )


def maybe_wrap_with_direction_ml(
    base_policy: EntryPolicy,
    *,
    model_path: Optional[str] = None,
    ml_weight: float = _ML_WEIGHT_DEFAULT,
) -> EntryPolicy:
    """
    Returns a DirectionMLConflictResolver wrapping base_policy if a model path
    is configured, otherwise returns base_policy unchanged.

    Reads env vars:
        DIRECTION_ML_MODEL_PATH        — path to direction_only_model.joblib
        DIRECTION_ML_WEIGHT            — 0.0–1.0 blend weight (default 0.40)
        DIRECTION_ML_FILTER_MIN_PROB   — optional direction filter threshold (0–1)
                                         set to e.g. 0.52 to block low-confidence directions
    """
    path = model_path or os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
    if not path:
        return base_policy

    env_weight = os.getenv("DIRECTION_ML_WEIGHT", "").strip()
    try:
        ml_weight = float(env_weight) if env_weight else ml_weight
    except ValueError:
        pass
    ml_weight = max(0.0, min(1.0, ml_weight))

    filter_env = os.getenv("DIRECTION_ML_FILTER_MIN_PROB", "").strip()
    filter_min_prob: Optional[float] = None
    if filter_env:
        try:
            filter_min_prob = float(filter_env)
            if not (0.0 < filter_min_prob < 1.0):
                logger.warning("DIRECTION_ML_FILTER_MIN_PROB=%s is out of (0,1) range — ignoring", filter_env)
                filter_min_prob = None
        except ValueError:
            pass

    bundle = _load_bundle(path)
    if bundle is None:
        logger.warning("direction_ml_policy: could not load bundle from %s — using base policy", path)
        return base_policy

    logger.info(
        "direction_ml_policy: wrapping %s  weight=%.2f  filter=%s",
        type(base_policy).__name__,
        ml_weight,
        f"{filter_min_prob:.2f}" if filter_min_prob else "off",
    )
    return DirectionMLConflictResolver(
        base_policy,
        bundle,
        ml_weight=ml_weight,
        filter_min_prob=filter_min_prob,
    )
