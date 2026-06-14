"""Pipeline node implementations.

Each node wraps one model/classifier and returns a string output key
that the executor uses to route to the next node. Every node is:
  - stateless per-bar (safe to call in parallel/replay)
  - safe on missing data (returns a default key, never raises)
  - independently testable (accepts a mock SnapshotAccessor)

Model registry:
  "regime_classifier"  → RegimeNode
  "big_move_model"     → BigMoveNode   (entry_only_v3 bundle)
  "direction_model"    → DirectionNode  (RegimeDirector weighted detector)
  "seller_model"       → SellerNode     (SellerBrain IV + regime gate)
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..market.snapshot_accessor import SnapshotAccessor
from .config import NodeConfig

logger = logging.getLogger(__name__)


# ── base ──────────────────────────────────────────────────────────────────────

class PipelineNode(ABC):
    """Evaluate a snapshot and return the routing key for the next node."""

    @abstractmethod
    def evaluate(self, snap: SnapshotAccessor) -> str: ...

    @property
    @abstractmethod
    def node_name(self) -> str: ...


# ── regime node ───────────────────────────────────────────────────────────────

class RegimeNode(PipelineNode):
    """Maps RegimeClassifier output to TREND_UP / TREND_DOWN / RANGE / HIGH_VOL / LOW_VOL.

    Our Regime enum (TRENDING/SIDEWAYS/BREAKOUT/...) is richer than the pipeline
    labels. The reason string carries BULL/BEAR direction inside TRENDING so we can
    split to TREND_UP vs TREND_DOWN without a separate call.

    Mapping:
      TRENDING  + reason contains BULL  → TREND_UP
      TRENDING  + reason contains BEAR  → TREND_DOWN
      SIDEWAYS, CHOP                    → RANGE
      HIGH_VOL, PANIC, BREAKOUT         → HIGH_VOL
      DEAD_MARKET, AVOID, EXPIRY, etc.  → LOW_VOL   (safe-exit)
    """

    # Override via node params: params.regime_map: {TRENDING: HIGH_VOL, ...}
    _DEFAULT_MAP = {
        "TREND_UP": "TREND_UP",
        "TREND_DOWN": "TREND_DOWN",
        "SIDEWAYS": "RANGE",
        "CHOP": "RANGE",
        "HIGH_VOL": "HIGH_VOL",
        "BREAKOUT": "HIGH_VOL",
        "PANIC": "HIGH_VOL",
        "DEAD_MARKET": "LOW_VOL",
        "AVOID": "LOW_VOL",
        "PRE_EXPIRY": "LOW_VOL",
        "EXPIRY": "LOW_VOL",
    }

    def __init__(self, name: str, config: NodeConfig) -> None:
        from ..market.regime import RegimeClassifier
        self._name = name
        self._classifier = RegimeClassifier()
        self._map = dict(self._DEFAULT_MAP)
        self._map.update(config.params.get("regime_map") or {})

    @property
    def node_name(self) -> str:
        return self._name

    def evaluate(self, snap: SnapshotAccessor) -> str:
        try:
            signal = self._classifier.classify(snap)
            regime_str = signal.regime.value  # e.g. "TRENDING"
            reason_upper = signal.reason.upper()

            # For TRENDING, split on direction from reason string
            if regime_str == "TRENDING":
                if "BULL" in reason_upper:
                    return self._map.get("TREND_UP", "TREND_UP")
                elif "BEAR" in reason_upper:
                    return self._map.get("TREND_DOWN", "TREND_DOWN")
                # Ambiguous trend — map same as TREND_UP (we still route to volatility)
                return self._map.get("TREND_UP", "TREND_UP")

            return self._map.get(regime_str, "LOW_VOL")
        except Exception:
            logger.debug("regime node %s: classify failed", self._name, exc_info=True)
            return "LOW_VOL"


# ── big-move (opportunity / magnitude) node ───────────────────────────────────

class BigMoveNode(PipelineNode):
    """Gates on P(big move) from the entry_only_v3 ML bundle.

    Output keys: "pass" (prob >= threshold) | "fail"
    Model path: ENTRY_ML_MODEL_PATH env var, or params.model_path.
    """

    def __init__(self, name: str, config: NodeConfig) -> None:
        self._name = name
        self._threshold = config.threshold if config.threshold is not None else 0.85
        # max_nan_features: refuse to score if this many features are NaN.
        # Blocks market-open bars (velocity/delta features need history) and
        # stale snapshot formats. Default 15 allows the known 8 structural NaNs
        # on live GCP while blocking 40+ NaN bars from old-format snapshots.
        self._max_nan = int(config.params.get("max_nan_features", 15))
        self._bundle: Optional[Dict[str, Any]] = None
        model_path = (
            config.params.get("model_path")
            or os.getenv("ENTRY_ML_MODEL_PATH", "").strip()
        )
        if model_path:
            self._bundle = self._load(model_path)

    @property
    def node_name(self) -> str:
        return self._name

    @staticmethod
    def _load(path: str) -> Optional[Dict[str, Any]]:
        try:
            from ..ml.bundle_inference import load_joblib_bundle
            return load_joblib_bundle(path, expected_kind="entry_only_bundle")
        except Exception:
            logger.exception("big_move node: failed to load bundle %s", path)
            return None

    def evaluate(self, snap: SnapshotAccessor) -> str:
        if self._bundle is None:
            logger.debug("big_move node %s: no bundle — defaulting to fail", self._name)
            return "fail"
        try:
            from ..ml.bundle_inference import predict_positive_class_prob
            prob = predict_positive_class_prob(self._bundle, snap, max_nan_features=self._max_nan)
            if prob is None:
                return "fail"
            result = "pass" if prob >= self._threshold else "fail"
            logger.debug("big_move %s: prob=%.3f thr=%.2f → %s", self._name, prob, self._threshold, result)
            return result
        except Exception:
            logger.debug("big_move node %s: predict failed", self._name, exc_info=True)
            return "fail"


# ── direction node ─────────────────────────────────────────────────────────────

class DirectionNode(PipelineNode):
    """CE vs PE decision via RegimeDirector weighted signal.

    Output keys:
      "bullish"   (CE, confidence >= threshold)
      "bearish"   (PE, confidence >= threshold)
      "uncertain" (ABSTAIN, or confidence below threshold)

    Threshold governs the confidence gate from the weighted detector.
    The REGIME_W_MOM=0 env var should already be set (momentum is anti-signal).
    """

    def __init__(self, name: str, config: NodeConfig) -> None:
        self._name = name
        self._threshold = config.threshold if config.threshold is not None else 0.65
        signal = config.params.get("signal") or os.getenv("REGIME_DIRECTION_SIGNAL", "weighted")
        from ..brain.regime_director import RegimeDirector
        self._director = RegimeDirector(signal=signal)

    @property
    def node_name(self) -> str:
        return self._name

    def evaluate(self, snap: SnapshotAccessor) -> str:
        try:
            verdict = self._director.decide(snap)
            if verdict.side == "CE" and verdict.confidence >= self._threshold:
                return "bullish"
            if verdict.side == "PE" and verdict.confidence >= self._threshold:
                return "bearish"
            return "uncertain"
        except Exception:
            logger.debug("direction node %s: decide failed", self._name, exc_info=True)
            return "uncertain"


# ── seller node ───────────────────────────────────────────────────────────────

class SellerNode(PipelineNode):
    """SellerBrain wrapper — routes by whether the seller wants to fire.

    Output keys:
      "high_confidence"  (seller fires: IV gate passed, regime allows)
      "low_confidence"   (seller abstains: IV too low or regime wrong)

    threshold here is used as an IV-rank floor override (default: SELLER_IV_RANK_MIN).
    """

    def __init__(self, name: str, config: NodeConfig) -> None:
        self._name = name
        iv_override = config.threshold  # threshold repurposed as IV-rank min
        from ..seller.brain import SellerBrain
        self._brain = SellerBrain(
            iv_rank_min=iv_override if iv_override is not None else None,
        )

    @property
    def node_name(self) -> str:
        return self._name

    def evaluate(self, snap: SnapshotAccessor) -> str:
        try:
            decision = self._brain.decide(snap)
            return "high_confidence" if decision.fires else "low_confidence"
        except Exception:
            logger.debug("seller node %s: decide failed", self._name, exc_info=True)
            return "low_confidence"


# ── terminal node ─────────────────────────────────────────────────────────────

class TerminalNode(PipelineNode):
    """No-op node — evaluate() returns the strategy/action for the executor to read."""

    def __init__(self, name: str, config: NodeConfig) -> None:
        self._name = name
        # Terminal action: strategy name takes priority over action
        self._action = config.strategy or config.action or "no_trade"

    @property
    def node_name(self) -> str:
        return self._name

    def evaluate(self, snap: SnapshotAccessor) -> str:
        return self._action


# ── registry ──────────────────────────────────────────────────────────────────

_MODEL_REGISTRY: Dict[str, type] = {
    "regime_classifier": RegimeNode,
    "big_move_model": BigMoveNode,
    "direction_model": DirectionNode,
    "seller_model": SellerNode,
}


def build_node(name: str, config: NodeConfig) -> PipelineNode:
    """Instantiate the correct node class from the config."""
    if config.is_terminal:
        return TerminalNode(name, config)
    if config.model is None:
        raise ValueError(f"non-terminal node '{name}' must specify a 'model'")
    cls = _MODEL_REGISTRY.get(config.model)
    if cls is None:
        raise ValueError(
            f"node '{name}' references unknown model '{config.model}'. "
            f"Known: {sorted(_MODEL_REGISTRY)}"
        )
    return cls(name, config)
