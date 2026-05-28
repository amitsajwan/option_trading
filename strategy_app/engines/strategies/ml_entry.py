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
from ...ml.entry_direction_resolver import resolve_entry_direction, resolve_entry_direction_momentum
from ...utils.env import env_bool

logger = logging.getLogger(__name__)

STRATEGY_NAME = "ML_ENTRY"
_ENTRY_BUNDLE_KIND = "entry_only_bundle"
_DIRECTION_BUNDLE_KIND = "direction_only_bundle"
_DIRECTION_DUAL_BUNDLE_KIND = "direction_dual_bundle"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _load_dir_bundle(path: str) -> Optional[dict[str, Any]]:
    """Load direction bundle — accepts direction_only_bundle or direction_dual_bundle."""
    try:
        import joblib
        bundle = joblib.load(path)
        if not isinstance(bundle, dict):
            return None
        kind = bundle.get("kind", "")
        if kind in (_DIRECTION_BUNDLE_KIND, _DIRECTION_DUAL_BUNDLE_KIND):
            return bundle
        logger.warning("ml_entry: unexpected direction bundle kind=%s at %s", kind, path)
        return None
    except Exception:
        logger.exception("ml_entry: failed to load direction bundle %s", path)
        return None


def _resolve_direction_dual(bundle: dict[str, Any], snap: SnapshotAccessor) -> Optional[Direction]:
    """Pick CE or PE from dual bundle.

    Default (DIRECTION_DUAL_MIN_PROB=0): argmax like unified direction_only — always
    picks CE or PE. E3-S6 replay showed strict dual gate (both < 0.5) silenced all votes.

    Set DIRECTION_DUAL_MIN_PROB=0.5 to restore the original strict gate.
    """
    min_prob = _env_float("DIRECTION_DUAL_MIN_PROB", 0.0)
    ce_sub = bundle.get("ce_bundle")
    pe_sub = bundle.get("pe_bundle")
    ce_win = predict_positive_class_prob(ce_sub, snap) if isinstance(ce_sub, dict) else None
    pe_win = predict_positive_class_prob(pe_sub, snap) if isinstance(pe_sub, dict) else None

    if ce_win is None and pe_win is None:
        return None
    if pe_win is None:
        if min_prob > 0 and (ce_win or 0.0) < min_prob:
            return None
        return Direction.CE
    if ce_win is None:
        if min_prob > 0 and (pe_win or 0.0) < min_prob:
            return None
        return Direction.PE
    if ce_win >= pe_win:
        if min_prob > 0 and ce_win < min_prob:
            return None
        return Direction.CE
    if min_prob > 0 and pe_win < min_prob:
        return None
    return Direction.PE


def _apply_direction_block(
    direction: Optional[Direction],
    source: str,
) -> tuple[Optional[Direction], str]:
    if direction is None:
        return None, source
    if env_bool("ML_ENTRY_BLOCK_CE") and direction == Direction.CE:
        return None, f"{source}+block_ce"
    if env_bool("ML_ENTRY_BLOCK_PE") and direction == Direction.PE:
        return None, f"{source}+block_pe"
    return direction, source


def _resolve_direction(snap: SnapshotAccessor) -> tuple[Optional[Direction], str]:
    """CE/PE for ML_ENTRY. Returns (direction_or_None, source_label)."""
    if env_bool("ML_ENTRY_PE_ONLY"):
        return Direction.PE, "pe_only"
    if env_bool("ML_ENTRY_CE_ONLY"):
        return Direction.CE, "ce_only"

    direction: Optional[Direction]
    dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
    if dir_path:
        bundle = _load_dir_bundle(dir_path)
        if bundle is not None:
            if bundle.get("kind") == _DIRECTION_DUAL_BUNDLE_KIND:
                direction = _resolve_direction_dual(bundle, snap)
                return _apply_direction_block(direction, "direction_dual_ml")
            # single direction_only_bundle
            ce_prob = predict_positive_class_prob(bundle, snap)
            if ce_prob is not None:
                direction = Direction.CE if ce_prob >= 0.5 else Direction.PE
                return _apply_direction_block(direction, "direction_ml")
    ret5 = snap.fut_return_5m
    if ret5 is not None and ret5 != 0:
        direction = Direction.CE if float(ret5) > 0 else Direction.PE
    else:
        direction = Direction.CE
    return _apply_direction_block(direction, "momentum")


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

        if env_bool("ML_ENTRY_PE_ONLY"):
            direction = Direction.PE
            raw_signals = {
                "entry_prob": round(entry_prob, 4),
                "entry_threshold": self._min_prob,
                "direction_source": "pe_only",
            }
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
                proposed_entry_premium=snap.atm_pe_close,
            )
        if env_bool("ML_ENTRY_CE_ONLY"):
            direction = Direction.CE
            raw_signals = {
                "entry_prob": round(entry_prob, 4),
                "entry_threshold": self._min_prob,
                "direction_source": "ce_only",
            }
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
                proposed_entry_premium=snap.atm_ce_close,
            )

        direction_mode = os.getenv("ML_ENTRY_DIRECTION_MODE", "composite").strip().lower()
        raw_signals: dict[str, Any] = {
            "entry_prob": round(entry_prob, 4),
            "entry_threshold": self._min_prob,
            "_entry_policy_mode": "bypass",
        }

        if direction_mode == "consensus":
            hint_dir, hint_source = _resolve_direction(snap)
            ce_prob: Optional[float] = None
            dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
            if dir_path:
                dir_bundle = _load_dir_bundle(dir_path)
                if dir_bundle is not None and dir_bundle.get("kind") == _DIRECTION_BUNDLE_KIND:
                    ce_prob = predict_positive_class_prob(dir_bundle, snap)
            raw_signals.update(
                {
                    "_ml_entry_timing_only": True,
                    "direction_source": "ml_entry_timing",
                    "ml_direction_hint": hint_dir.value if hint_dir else None,
                    "ml_direction_ce_prob": round(ce_prob, 4) if ce_prob is not None else None,
                    "ml_direction_hint_source": hint_source,
                }
            )
            direction = hint_dir or Direction.CE
        elif direction_mode in {"legacy", "direction_ml", "bind"}:
            direction, direction_source = _resolve_direction(snap)
            if direction is None:
                return None
            raw_signals["direction_source"] = direction_source
        elif direction_mode in {"momentum", "mom"}:
            dir_result = resolve_entry_direction_momentum(snap)
            if dir_result.vetoed or dir_result.direction is None:
                return None
            direction = dir_result.direction
            raw_signals.update(dir_result.as_raw_signals())
        else:
            dir_result = resolve_entry_direction(snap)
            if dir_result.vetoed or dir_result.direction is None:
                return None
            direction = dir_result.direction
            raw_signals.update(dir_result.as_raw_signals())

        direction, block_tag = _apply_direction_block(
            direction,
            str(raw_signals.get("direction_source") or ""),
        )
        if direction is None:
            return None
        if block_tag != str(raw_signals.get("direction_source") or ""):
            raw_signals["direction_source"] = block_tag

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
