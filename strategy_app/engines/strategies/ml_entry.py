"""ML-only entry strategy: emit ENTRY votes from the Stage-1 entry model.

The entry *trigger* is the ML probability gate; the *direction* is resolved by
the shared :mod:`entry_direction_policy` (so VOL_GATE_ENTRY can reuse identical
direction logic with a different trigger).
"""
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
from .entry_direction_policy import _env_float, resolve_direction_for_entry

logger = logging.getLogger(__name__)

STRATEGY_NAME = "ML_ENTRY"
_ENTRY_BUNDLE_KIND = "entry_only_bundle"


class MlEntryStrategy(BaseStrategy):
    """Entry votes driven by ENTRY_ML_MODEL_PATH (Stage-1 research export)."""

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
        # Record the prob EVERY bar (incl. declines) so the trace captures the
        # full distribution for separation analysis — before the threshold gate.
        try:
            from ...runtime.eval_context import set_entry_diag
            set_entry_diag({
                "entry_prob": round(float(entry_prob), 4),
                "threshold": round(float(self._min_prob), 4),
                "fired": bool(entry_prob >= self._min_prob),
                "snapshot_id": snap.snapshot_id,
            })
        except Exception:
            pass
        if entry_prob < self._min_prob:
            return None

        direction, raw_signals = resolve_direction_for_entry(snap)
        if direction is None:
            return None
        raw_signals = {
            "entry_prob": round(entry_prob, 4),
            "entry_threshold": self._min_prob,
            **raw_signals,
        }
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
            raw_signals=raw_signals,
            proposed_strike=snap.atm_strike,
            proposed_entry_premium=premium,
        )
