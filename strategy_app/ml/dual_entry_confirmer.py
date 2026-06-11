"""DualEntryConfirmer — STEP 2 of the dual-model entry.

Given a SIDE already chosen by the RegimeDirector (step 1), call ONLY that side's
signed model to confirm a move of X% is likely in that direction:
  * CE bundle (entry_bn_5m_up_v1)   -> P(forward HIGH clears +X%)
  * PE bundle (entry_bn_5m_down_v1) -> P(forward LOW  clears -X%)

The model does NOT pick direction (the RegimeDirector did). It only confirms
directional magnitude, so its precision is measured on the conditioned subset
(side already chosen), not the unconditional ~40%.

Both bundles are ordinary ``entry_only_bundle`` joblibs (kind/features/model/
feature_medians) loaded via the shared bundle_inference path. Never raises.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..market.snapshot_accessor import SnapshotAccessor
from .bundle_inference import load_joblib_bundle, predict_positive_class_prob

logger = logging.getLogger(__name__)

_ENTRY_BUNDLE_KIND = "entry_only_bundle"
_BUNDLE_CACHE: Dict[str, Any] = {}  # path -> bundle | None (don't retry failures)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _load(path: str) -> Optional[dict]:
    if not path:
        return None
    if path not in _BUNDLE_CACHE:
        _BUNDLE_CACHE[path] = load_joblib_bundle(path, expected_kind=_ENTRY_BUNDLE_KIND)
    b = _BUNDLE_CACHE[path]
    return b if isinstance(b, dict) else None


@dataclass
class ConfirmVerdict:
    fire: bool
    prob: Optional[float]
    threshold: float
    side: str
    model_loaded: bool
    reason: str

    def as_raw_signals(self) -> Dict[str, Any]:
        return {
            "dual_confirm_side": self.side,
            "dual_confirm_prob": round(self.prob, 4) if self.prob is not None else None,
            "dual_confirm_threshold": round(self.threshold, 4),
            "dual_confirm_fire": bool(self.fire),
            "dual_confirm_model_loaded": bool(self.model_loaded),
        }


class DualEntryConfirmer:
    """Step-2 magnitude-in-direction confirmation for a pre-chosen side."""

    def __init__(
        self,
        ce_path: Optional[str] = None,
        pe_path: Optional[str] = None,
        ce_min_prob: Optional[float] = None,
        pe_min_prob: Optional[float] = None,
    ) -> None:
        self.ce_path = (ce_path if ce_path is not None else os.getenv("ENTRY_CE_MODEL_PATH", "")).strip()
        self.pe_path = (pe_path if pe_path is not None else os.getenv("ENTRY_PE_MODEL_PATH", "")).strip()
        self.ce_min_prob = ce_min_prob if ce_min_prob is not None else _env_float("ENTRY_CE_MIN_PROB", 0.50)
        self.pe_min_prob = pe_min_prob if pe_min_prob is not None else _env_float("ENTRY_PE_MIN_PROB", 0.50)

    def confirm(self, side: str, snap: SnapshotAccessor) -> ConfirmVerdict:
        side = (side or "").upper()
        if side not in ("CE", "PE"):
            return ConfirmVerdict(False, None, 0.0, side, False, "side not CE/PE")
        path = self.ce_path if side == "CE" else self.pe_path
        thr = self.ce_min_prob if side == "CE" else self.pe_min_prob
        bundle = _load(path)
        if bundle is None:
            return ConfirmVerdict(False, None, thr, side, False, f"{side} bundle not loaded ({path or 'unset'})")
        prob = predict_positive_class_prob(bundle, snap)
        if prob is None:
            return ConfirmVerdict(False, None, thr, side, True, "predict returned None")
        fire = prob >= thr
        return ConfirmVerdict(fire, prob, thr, side, True, f"{side} prob={prob:.3f} {'>=' if fire else '<'}{thr:.2f}")
