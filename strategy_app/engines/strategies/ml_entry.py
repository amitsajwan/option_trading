"""ML-only entry strategy: emit ENTRY votes from Stage-1 entry model (no rule conditions)."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ...contracts import (
    BaseStrategy,
    Direction,
    PositionContext,
    RiskContext,
    SignalType,
    SnapshotPayload,
    StrategyVote,
)
from ...market.snapshot_accessor import SnapshotAccessor
from ...ml.bundle_inference import load_joblib_bundle, predict_positive_class_prob

logger = logging.getLogger(__name__)

STRATEGY_NAME = "ML_ENTRY"
_ENTRY_BUNDLE_KIND = "entry_only_bundle"
_DIRECTION_BUNDLE_KIND = "direction_only_bundle"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_direction(snap: SnapshotAccessor) -> Direction:
    dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
    if dir_path:
        bundle = load_joblib_bundle(dir_path, expected_kind=_DIRECTION_BUNDLE_KIND)
        if bundle is not None:
            ce_prob = predict_positive_class_prob(bundle, snap)
            if ce_prob is not None:
                return Direction.CE if ce_prob >= 0.5 else Direction.PE
    ret5 = snap.fut_return_5m
    if ret5 is not None and ret5 != 0:
        return Direction.CE if float(ret5) > 0 else Direction.PE
    return Direction.CE


class MlEntryStrategy(BaseStrategy):
    """Entry votes driven only by ENTRY_ML_MODEL_PATH (Stage-1 research export)."""

    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._entry_bundle: Optional[dict[str, Any]] = None
        self._entry_path: str = ""
        self._min_prob: float = _env_float("ENTRY_ML_MIN_PROB", 0.55)

    def _ensure_entry_bundle(self) -> Optional[dict[str, Any]]:
        path = os.getenv("ENTRY_ML_MODEL_PATH", "").strip()
        if not path:
            return None
        if self._entry_bundle is not None and path == self._entry_path:
            return self._entry_bundle
        bundle = load_joblib_bundle(path, expected_kind=_ENTRY_BUNDLE_KIND)
        if bundle is None:
            self._entry_bundle = None
            self._entry_path = ""
            return None
        self._entry_bundle = bundle
        self._entry_path = path
        holdout_auc = (bundle.get("holdout_eval") or {}).get("roc_auc")
        logger.info(
            "ml_entry: loaded entry model path=%s features=%d holdout_auc=%s min_prob=%.2f",
            path,
            len(bundle.get("features") or []),
            holdout_auc,
            self._min_prob,
        )
        return self._entry_bundle

    def evaluate(
        self,
        snapshot: SnapshotPayload,
        position: Optional[PositionContext],
        risk: RiskContext,
    ) -> Optional[StrategyVote]:
        if position is not None:
            return None
        bundle = self._ensure_entry_bundle()
        if bundle is None:
            return None
        snap = SnapshotAccessor(snapshot)
        entry_prob = predict_positive_class_prob(bundle, snap)
        if entry_prob is None:
            return None
        if entry_prob < self._min_prob:
            return None
        direction = _resolve_direction(snap)
        premium = snap.atm_ce_close if direction == Direction.CE else snap.atm_pe_close
        return StrategyVote(
            strategy_name=self.name,
            snapshot_id=snap.snapshot_id,
            timestamp=snap.timestamp_or_now,
            trade_date=snap.trade_date,
            signal_type=SignalType.ENTRY,
            direction=direction,
            confidence=round(min(1.0, entry_prob), 3),
            reason=f"ml_entry: prob={entry_prob:.3f}>={self._min_prob:.2f}",
            raw_signals={
                "entry_prob": round(entry_prob, 4),
                "entry_threshold": self._min_prob,
                "direction_source": "direction_ml" if os.getenv("DIRECTION_ML_MODEL_PATH", "").strip() else "momentum",
                # ML_ENTRY owns its entry decision via the prob >= min_prob
                # gate above; bypass the engine's secondary entry-policy check
                # so the well-calibrated model signal isn't second-guessed by
                # a policy that was tuned for a different label scheme.
                "_entry_policy_mode": "bypass",
            },
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )
