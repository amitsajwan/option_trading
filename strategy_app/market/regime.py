"""Market regime classification for deterministic strategy routing."""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any, Optional

from .snapshot_accessor import SnapshotAccessor
from ..utils.env import env_float

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    TRENDING = "TRENDING"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOL = "HIGH_VOL"
    AVOID = "AVOID"
    PRE_EXPIRY = "PRE_EXPIRY"
    EXPIRY = "EXPIRY"
    # Phase 3 additions — canonical regime labels for stream-native pipeline
    CHOP = "CHOP"          # Low-vol, mixed returns, high candle overlap
    BREAKOUT = "BREAKOUT"  # ORH/ORL just broken with strong vol alignment
    PANIC = "PANIC"        # Fast intraday vol spike without full VIX halt
    DEAD_MARKET = "DEAD_MARKET"  # Near-zero participation; no tradeable move


class RegimeSignal:
    """Classification result with evidence for observability."""

    def __init__(self, *, regime: Regime, confidence: float, reason: str, evidence: dict[str, Any]) -> None:
        self.regime = regime
        self.confidence = float(confidence)
        self.reason = str(reason)
        self.evidence = dict(evidence)


class RegimeClassifier:
    """Phase-1 rule classifier with optional Phase-3 model fallback."""

    def __init__(self, *, model_path: Optional[str] = None, model_confidence_threshold: float = 0.70) -> None:
        self._model = None
        self._use_model = False
        self._model_confidence_threshold = float(model_confidence_threshold)
        self._trend_return_min = env_float("REGIME_TREND_RETURN_MIN", 0.0010) or 0.0010
        self._trend_vol_ratio_min = env_float("REGIME_TREND_VOL_RATIO_MIN", 1.30) or 1.30
        self._high_vol_vix_min = env_float("REGIME_HIGH_VOL_VIX_MIN", 22.0) or 22.0
        self._high_vol_rvol_min = env_float("REGIME_HIGH_VOL_RVOL_MIN", 0.015) or 0.015
        # Phase 3 thresholds
        self._panic_vix_chg_min = env_float("REGIME_PANIC_VIX_CHG_MIN", 5.0) or 5.0
        self._panic_rvol_min = env_float("REGIME_PANIC_RVOL_MIN", 0.012) or 0.012
        self._chop_vol_ratio_max = env_float("REGIME_CHOP_VOL_RATIO_MAX", 0.90) or 0.90
        self._chop_candle_overlap_min = env_float("REGIME_CHOP_CANDLE_OVERLAP_MIN", 0.40) or 0.40
        self._dead_vol_ratio_max = env_float("REGIME_DEAD_VOL_RATIO_MAX", 0.30) or 0.30
        self._breakout_orw_pct_min = env_float("REGIME_BREAKOUT_ORW_PCT_MIN", 0.003) or 0.003
        if model_path:
            self._load_model(model_path)

    def configure(self, payload: Optional[dict[str, object]]) -> None:
        """Apply threshold overrides from run metadata."""
        if not isinstance(payload, dict):
            return
        if "trend_return_min" in payload:
            try:
                self._trend_return_min = float(payload["trend_return_min"])
            except (TypeError, ValueError):
                pass
        if "trend_vol_ratio_min" in payload:
            try:
                self._trend_vol_ratio_min = float(payload["trend_vol_ratio_min"])
            except (TypeError, ValueError):
                pass
        if "high_vol_vix_min" in payload:
            try:
                self._high_vol_vix_min = float(payload["high_vol_vix_min"])
            except (TypeError, ValueError):
                pass
        if "high_vol_rvol_min" in payload:
            try:
                self._high_vol_rvol_min = float(payload["high_vol_rvol_min"])
            except (TypeError, ValueError):
                pass
        for key, attr in (
            ("panic_vix_chg_min", "_panic_vix_chg_min"),
            ("panic_rvol_min", "_panic_rvol_min"),
            ("chop_vol_ratio_max", "_chop_vol_ratio_max"),
            ("chop_candle_overlap_min", "_chop_candle_overlap_min"),
            ("dead_vol_ratio_max", "_dead_vol_ratio_max"),
            ("breakout_orw_pct_min", "_breakout_orw_pct_min"),
        ):
            if key in payload:
                try:
                    setattr(self, attr, float(payload[key]))
                except (TypeError, ValueError):
                    pass

    def classify(self, snap: SnapshotAccessor) -> RegimeSignal:
        if self._use_model:
            model_signal = self._model_classify(snap)
            if model_signal is not None and model_signal.confidence >= self._model_confidence_threshold:
                return model_signal
        return self._rule_classify(snap)

    def _rule_classify(self, snap: SnapshotAccessor) -> RegimeSignal:
        hard_avoid = self._check_hard_avoid(snap)
        if hard_avoid is not None:
            return hard_avoid

        if snap.is_expiry_day:
            return RegimeSignal(
                regime=Regime.EXPIRY,
                confidence=0.90,
                reason="EXPIRY_DAY: weekly expiry regime",
                evidence={
                    "is_expiry_day": True,
                    "days_to_expiry": snap.days_to_expiry,
                    "max_pain": snap.max_pain,
                    "fut_close": snap.fut_close,
                },
            )

        if snap.days_to_expiry == 1:
            return RegimeSignal(
                regime=Regime.PRE_EXPIRY,
                confidence=0.80,
                reason="PRE_EXPIRY: one day to expiry",
                evidence={
                    "days_to_expiry": snap.days_to_expiry,
                    "iv_percentile": snap.iv_percentile,
                    "iv_regime": snap.iv_regime,
                },
            )

        panic = self._check_panic(snap)
        if panic is not None:
            return panic

        high_vol = self._check_high_vol(snap)
        if high_vol is not None:
            return high_vol

        dead = self._check_dead_market(snap)
        if dead is not None:
            return dead

        return self._classify_trend_vs_sideways(snap)

    def _check_hard_avoid(self, snap: SnapshotAccessor) -> Optional[RegimeSignal]:
        if snap.vix_spike_flag:
            return RegimeSignal(
                regime=Regime.AVOID,
                confidence=0.99,
                reason="VIX_SPIKE_FLAG: avoid new entries",
                evidence={
                    "vix_spike_flag": True,
                    "vix_current": snap.vix_current,
                    "vix_intraday_chg": snap.vix_intraday_chg,
                },
            )
        if snap.is_pre_close:
            return RegimeSignal(
                regime=Regime.AVOID,
                confidence=0.99,
                reason=f"SESSION_PHASE: {snap.session_phase}",
                evidence={"session_phase": snap.session_phase},
            )
        return None

    def _check_panic(self, snap: SnapshotAccessor) -> Optional[RegimeSignal]:
        """Fast intraday volatility spike — not yet at VIX halt level but untradeably fast."""
        vix_chg = snap.vix_intraday_chg
        rvol = snap.realized_vol_30m
        if vix_chg is None or rvol is None:
            return None
        if vix_chg > self._panic_vix_chg_min and rvol > self._panic_rvol_min:
            return RegimeSignal(
                regime=Regime.PANIC,
                confidence=0.88,
                reason=f"PANIC: vix_chg={vix_chg:.1f}% rvol={rvol:.4f}",
                evidence={"vix_intraday_chg": vix_chg, "realized_vol_30m": rvol},
            )
        return None

    def _check_dead_market(self, snap: SnapshotAccessor) -> Optional[RegimeSignal]:
        """Near-zero participation — volume too low to trade."""
        vol_ratio = snap.vol_ratio
        if vol_ratio is None:
            return None
        if vol_ratio < self._dead_vol_ratio_max:
            return RegimeSignal(
                regime=Regime.DEAD_MARKET,
                confidence=0.85,
                reason=f"DEAD_MARKET: vol_ratio={vol_ratio:.2f}",
                evidence={"vol_ratio": vol_ratio},
            )
        return None

    def _check_high_vol(self, snap: SnapshotAccessor) -> Optional[RegimeSignal]:
        rvol = snap.realized_vol_30m
        vix = snap.vix_current
        if rvol is None or vix is None:
            return None
        if rvol > self._high_vol_rvol_min and vix > self._high_vol_vix_min:
            return RegimeSignal(
                regime=Regime.HIGH_VOL,
                confidence=0.80,
                reason=f"HIGH_VOL: rvol={rvol:.4f} vix={vix:.1f}",
                evidence={"realized_vol_30m": rvol, "vix_current": vix, "vix_regime": snap.vix_regime},
            )
        return None

    def _classify_trend_vs_sideways(self, snap: SnapshotAccessor) -> RegimeSignal:
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        r30m = snap.fut_return_30m
        vol_ratio = snap.vol_ratio
        evidence: dict[str, Any] = {
            "r5m": r5m,
            "r15m": r15m,
            "r30m": r30m,
            "vol_ratio": vol_ratio,
            "pcr": snap.pcr,
            "fut_oi_change_30m": snap.fut_oi_change_30m,
            "orh_broken": snap.orh_broken,
            "orl_broken": snap.orl_broken,
        }

        if None in (r5m, r15m, r30m):
            # Warmup policy: require full 5m/15m/30m history before classifying trend.
            # When the regime gate is enabled, this can suppress entries during roughly
            # the first 30 session minutes until r30m becomes available.
            return RegimeSignal(
                regime=Regime.SIDEWAYS,
                confidence=0.60,
                reason="SIDEWAYS: insufficient returns history",
                evidence=evidence,
            )

        threshold = self._trend_return_min
        aligned_up = r5m > threshold and r15m > threshold and r30m > threshold
        aligned_down = r5m < -threshold and r15m < -threshold and r30m < -threshold
        strong_vol = vol_ratio is not None and vol_ratio > self._trend_vol_ratio_min
        oi_pct = None
        if snap.fut_oi_change_30m is not None and snap.fut_oi is not None and snap.fut_oi > 0:
            oi_pct = snap.fut_oi_change_30m / snap.fut_oi
        evidence["oi_change_pct"] = oi_pct

        bull_score = 0.0
        bear_score = 0.0
        reasons: list[str] = []

        if aligned_up:
            bull_score += 1.4
            reasons.append("returns_aligned_up")
        elif aligned_down:
            bear_score += 1.4
            reasons.append("returns_aligned_down")
        else:
            reasons.append("returns_mixed")

        if strong_vol:
            if bull_score > bear_score:
                bull_score += 0.5
            elif bear_score > bull_score:
                bear_score += 0.5
            reasons.append(f"strong_vol={vol_ratio:.2f}")
        elif vol_ratio is not None and vol_ratio < 1.0:
            bull_score *= 0.8
            bear_score *= 0.8
            reasons.append(f"weak_vol={vol_ratio:.2f}")

        if oi_pct is not None:
            if oi_pct > 0.02 and r15m > 0:
                bull_score += 0.8
                reasons.append(f"oi_long_buildup={oi_pct:.2%}")
            elif oi_pct > 0.02 and r15m < 0:
                bear_score += 0.8
                reasons.append(f"oi_short_buildup={oi_pct:.2%}")
            elif oi_pct < -0.02:
                bull_score *= 0.7
                bear_score *= 0.7
                reasons.append(f"oi_unwinding={oi_pct:.2%}")

        if snap.pcr is not None:
            if snap.pcr > 1.2:
                bull_score += 0.4
                reasons.append(f"pcr_bull={snap.pcr:.2f}")
            elif snap.pcr < 0.8:
                bear_score += 0.4
                reasons.append(f"pcr_bear={snap.pcr:.2f}")

        if snap.orh_broken:
            bull_score += 0.8
            reasons.append("orh_broken")
        elif snap.orl_broken:
            bear_score += 0.8
            reasons.append("orl_broken")

        evidence["bull_score"] = round(bull_score, 3)
        evidence["bear_score"] = round(bear_score, 3)

        # Enrich evidence with Phase 3 features when available
        candle_overlap = snap.candle_overlap
        orw_pct = snap.opening_range_width_pct
        evidence["candle_overlap"] = candle_overlap
        evidence["opening_range_width_pct"] = orw_pct

        trend_threshold = 2.0

        # BREAKOUT takes priority over TRENDING when orh/orl is freshly broken
        # with strong volume and a wide-enough opening range.
        orh_or_orl_broken = snap.orh_broken or snap.orl_broken
        orw_wide_enough = orw_pct is None or orw_pct >= self._breakout_orw_pct_min
        if orh_or_orl_broken and strong_vol and orw_wide_enough:
            confidence = 0.88 if (bull_score + bear_score) >= 2.5 else 0.75
            direction = "BULL" if snap.orh_broken else "BEAR"
            return RegimeSignal(
                regime=Regime.BREAKOUT,
                confidence=confidence,
                reason=f"BREAKOUT_{direction}: " + ", ".join(reasons),
                evidence=evidence,
            )

        if bull_score >= trend_threshold:
            confidence = 0.85 if bull_score >= 3.0 else 0.70
            return RegimeSignal(
                regime=Regime.TRENDING,
                confidence=confidence,
                reason="TRENDING_BULL: " + ", ".join(reasons),
                evidence=evidence,
            )
        if bear_score >= trend_threshold:
            confidence = 0.85 if bear_score >= 3.0 else 0.70
            return RegimeSignal(
                regime=Regime.TRENDING,
                confidence=confidence,
                reason="TRENDING_BEAR: " + ", ".join(reasons),
                evidence=evidence,
            )

        # CHOP: returns are mixed, vol is weak, and candle overlap is high
        returns_mixed = not (aligned_up or aligned_down)
        weak_vol = vol_ratio is not None and vol_ratio < self._chop_vol_ratio_max
        high_overlap = candle_overlap is not None and candle_overlap >= self._chop_candle_overlap_min
        if returns_mixed and (weak_vol or high_overlap):
            return RegimeSignal(
                regime=Regime.CHOP,
                confidence=0.72,
                reason="CHOP: " + ", ".join(reasons),
                evidence=evidence,
            )

        return RegimeSignal(
            regime=Regime.SIDEWAYS,
            confidence=0.65,
            reason="SIDEWAYS: " + ", ".join(reasons),
            evidence=evidence,
        )

    def _load_model(self, model_path: str) -> None:
        try:
            import joblib

            self._model = joblib.load(model_path)
            self._use_model = True
            logger.info("regime model loaded from %s", model_path)
        except Exception as exc:
            logger.warning("failed to load regime model path=%s error=%s", model_path, exc)
            self._use_model = False

    def _model_classify(self, snap: SnapshotAccessor) -> Optional[RegimeSignal]:
        if self._model is None:
            return None
        try:
            features = self._extract_model_features(snap)
            probabilities = self._model.predict_proba([features])[0]
            classes = self._model.classes_
            best_index = int(probabilities.argmax())
            best_label = classes[best_index]
            confidence = float(probabilities[best_index])
            return RegimeSignal(
                regime=Regime(str(best_label)),
                confidence=confidence,
                reason=f"ML_MODEL: {best_label} conf={confidence:.2f}",
                evidence={"probabilities": dict(zip(classes.tolist() if hasattr(classes, 'tolist') else list(classes), probabilities.tolist()))},
            )
        except Exception as exc:
            logger.warning("regime model inference failed: %s", exc)
            return None

    def _extract_model_features(self, snap: SnapshotAccessor) -> list[float]:
        return [
            float(snap.fut_return_5m or 0.0),
            float(snap.fut_return_15m or 0.0),
            float(snap.fut_return_30m or 0.0),
            float(snap.vol_ratio or 1.0),
            float(snap.realized_vol_30m or 0.0),
            float(snap.pcr or 1.0),
            float(snap.vix_current or 15.0),
            float(snap.iv_percentile or 50.0),
            float(snap.fut_oi_change_30m or 0.0),
            1.0 if snap.orh_broken else 0.0,
            1.0 if snap.orl_broken else 0.0,
            1.0 if snap.is_expiry_day else 0.0,
            float(snap.days_to_expiry or 7),
            float(snap.minutes or 0),
        ]
