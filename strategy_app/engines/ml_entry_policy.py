"""ML-backed entry policy that composes the deterministic entry gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import joblib
import pandas as pd
import json

from ..contracts import Direction, RiskContext, StrategyVote
from .entry_policy import EntryPolicy, EntryPolicyDecision, LongOptionEntryPolicy
from .regime import RegimeSignal
from .snapshot_accessor import SnapshotAccessor

ENTRY_MODEL_NUMERIC_COLUMNS = [
    "vote_confidence",
    "regime_confidence",
    "policy_score",
    "minutes_since_open",
    "days_to_expiry",
    "fut_return_5m",
    "fut_return_15m",
    "fut_return_30m",
    "vol_ratio",
    "realized_vol_30m",
    "pcr",
    "pcr_change_30m",
    "fut_oi_change_30m",
    "fut_oi",
    "oi_change_pct",
    "vix_current",
    "iv_percentile",
    "iv_skew",
    "price_vs_vwap",
    "or_width",
    "price_vs_orh",
    "price_vs_orl",
    "atm_ce_close",
    "atm_pe_close",
    "atm_ce_vol_ratio",
    "atm_pe_vol_ratio",
    "atm_ce_oi_change_30m",
    "atm_pe_oi_change_30m",
]

ENTRY_MODEL_CATEGORICAL_COLUMNS = [
    "strategy_name",
    "direction",
    "regime",
    "policy_check_volume",
    "policy_check_momentum",
    "policy_check_timing",
    "policy_check_premium",
    "policy_check_regime",
]

ENTRY_MODEL_FEATURE_COLUMNS = ENTRY_MODEL_NUMERIC_COLUMNS + ENTRY_MODEL_CATEGORICAL_COLUMNS

THRESHOLD_FIXED_060 = "fixed_060"
THRESHOLD_FIXED_065 = "fixed_065"
THRESHOLD_SEGMENT_OPTIMAL = "segment_optimal"
THRESHOLD_STRATEGY_OVERRIDE = "strategy_override_v1"

STRATEGY_OVERRIDE_THRESHOLDS = {
    "OI_BUILDUP": 0.62,
    "ORB": 0.66,
    "EMA_CROSSOVER": 0.80,
    "VWAP_RECLAIM": 0.62,
    "PREV_DAY_LEVEL": 0.67,
}

STRATEGY_REGIME_OVERRIDE_THRESHOLDS = {
    ("SIDEWAYS", "OI_BUILDUP"): 0.70,
    ("TRENDING", "OI_BUILDUP"): 0.64,
    ("PRE_EXPIRY", "OI_BUILDUP"): 0.66,
}


def _parse_custom_fixed_threshold_policy(policy_id: str) -> Optional[float]:
    text = str(policy_id or "").strip().lower()
    prefix = "fixed_custom_"
    if not text.startswith(prefix):
        return None
    raw = text[len(prefix) :].strip()
    if not raw:
        return None
    if raw.replace(".", "", 1).isdigit():
        if "." in raw:
            try:
                value = float(raw)
            except Exception:
                return None
        else:
            try:
                value = float(int(raw)) / 100.0
            except Exception:
                return None
    else:
        return None
    if value <= 0.0:
        return None
    if value >= 1.0:
        value = value / 100.0
    if value <= 0.0 or value >= 1.0:
        return None
    return float(value)


def _normalize_optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def build_entry_feature_row(
    *,
    snap: SnapshotAccessor,
    vote: StrategyVote,
    regime: RegimeSignal,
    base_decision: EntryPolicyDecision,
) -> dict[str, Any]:
    checks = dict(base_decision.checks or {})
    fut_oi = snap.fut_oi
    fut_oi_change_30m = snap.fut_oi_change_30m
    oi_change_pct = None
    if fut_oi is not None and fut_oi > 0 and fut_oi_change_30m is not None:
        oi_change_pct = float(fut_oi_change_30m) / float(fut_oi)
    return {
        "vote_confidence": float(vote.confidence),
        "regime_confidence": float(regime.confidence),
        "policy_score": float(base_decision.score),
        "minutes_since_open": snap.minutes,
        "days_to_expiry": snap.days_to_expiry,
        "fut_return_5m": snap.fut_return_5m,
        "fut_return_15m": snap.fut_return_15m,
        "fut_return_30m": snap.fut_return_30m,
        "vol_ratio": snap.vol_ratio,
        "realized_vol_30m": snap.realized_vol_30m,
        "pcr": snap.pcr,
        "pcr_change_30m": snap.pcr_change_30m,
        "fut_oi_change_30m": fut_oi_change_30m,
        "fut_oi": fut_oi,
        "oi_change_pct": oi_change_pct,
        "vix_current": snap.vix_current,
        "iv_percentile": snap.iv_percentile,
        "iv_skew": snap.iv_skew,
        "price_vs_vwap": snap.price_vs_vwap,
        "or_width": snap.or_width,
        "price_vs_orh": snap.price_vs_orh,
        "price_vs_orl": snap.price_vs_orl,
        "atm_ce_close": snap.atm_ce_close,
        "atm_pe_close": snap.atm_pe_close,
        "atm_ce_vol_ratio": snap.atm_ce_vol_ratio,
        "atm_pe_vol_ratio": snap.atm_pe_vol_ratio,
        "atm_ce_oi_change_30m": snap.atm_ce_oi_change_30m,
        "atm_pe_oi_change_30m": snap.atm_pe_oi_change_30m,
        "strategy_name": str(vote.strategy_name or ""),
        "direction": vote.direction.value if vote.direction is not None else "",
        "regime": str(regime.regime.value or ""),
        "policy_check_volume": str(checks.get("volume") or "__MISSING__"),
        "policy_check_momentum": str(checks.get("momentum") or "__MISSING__"),
        "policy_check_timing": str(checks.get("timing") or "__MISSING__"),
        "policy_check_premium": str(checks.get("premium") or "__MISSING__"),
        "policy_check_regime": str(checks.get("regime") or "__MISSING__"),
    }


class MLEntryPolicy:
    """Compose deterministic gating with a segmented ML quality scorer."""

    def __init__(
        self,
        *,
        model_package_path: str | Path,
        base_policy: Optional[EntryPolicy] = None,
        default_threshold: Optional[float] = None,
        strategy_threshold_overrides: Optional[dict[str, float]] = None,
        strategy_regime_threshold_overrides: Optional[dict[tuple[str, str], float]] = None,
    ) -> None:
        self._base_policy = base_policy or LongOptionEntryPolicy()
        self._package_path = Path(model_package_path)
        self._bundle = joblib.load(self._package_path)
        self._default_threshold = float(default_threshold) if default_threshold is not None else None
        self._strategy_threshold_overrides = {
            str(key or "").strip().upper(): float(value)
            for key, value in (strategy_threshold_overrides or {}).items()
        }
        self._strategy_regime_threshold_overrides = {
            (str(regime or "").strip().upper(), str(strategy or "").strip().upper()): float(value)
            for (regime, strategy), value in (strategy_regime_threshold_overrides or {}).items()
        }

    def can_resolve_direction_conflict(self) -> bool:
        """Allow engine-level CE/PE conflicts to be broken by ML score."""
        return True

    @classmethod
    def from_registry(
        cls,
        *,
        registry_path: str | Path,
        experiment_id: str,
        base_policy: Optional[EntryPolicy] = None,
        threshold_policy_override: Optional[str] = None,
    ) -> "MLEntryPolicy":
        registry = pd.read_csv(Path(registry_path))
        required_columns = {"experiment_id", "threshold_policy_id"}
        missing = required_columns.difference(registry.columns)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"registry missing required columns: {missing_list}")

        experiment_key = str(experiment_id or "").strip()
        if not experiment_key:
            raise ValueError("experiment_id must be non-empty")

        matches = registry[registry["experiment_id"].astype("string").str.strip() == experiment_key].copy()
        if matches.empty:
            raise KeyError(f"experiment_id not found in registry: {experiment_key}")
        if len(matches.index) > 1:
            raise ValueError(f"registry contains duplicate experiment_id rows: {experiment_key}")

        row = matches.iloc[0]
        if "status" in matches.columns:
            status = str(row.get("status") or "").strip().lower()
            if status and status != "trained":
                raise ValueError(f"experiment_id is not deployable status={status}: {experiment_key}")

        bundle_path = _normalize_registry_bundle_path(row)
        package_path = Path(bundle_path)
        if not package_path.exists():
            raise FileNotFoundError(f"bundle_path does not exist for experiment_id={experiment_key}: {package_path}")

        selected_policy = _normalize_optional_text(threshold_policy_override) or str(row["threshold_policy_id"])
        threshold_kwargs = cls._threshold_kwargs_for_policy(str(selected_policy))
        return cls(
            model_package_path=package_path,
            base_policy=base_policy,
            **threshold_kwargs,
        )

    def evaluate(
        self,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        base_decision = self._base_policy.evaluate(snap, vote, regime, risk)
        if not base_decision.allowed:
            return base_decision

        segment = self._resolve_segment(regime.regime.value)
        if segment is None:
            checks = dict(base_decision.checks)
            checks["ml_segment"] = "BLOCK:no_segment"
            return EntryPolicyDecision.block(f"ml: no segment model for regime={regime.regime.value}", checks)

        row = build_entry_feature_row(snap=snap, vote=vote, regime=regime, base_decision=base_decision)
        raw_score, calibrated_score = self._score_row(row, segment)
        threshold = self._resolve_threshold(
            strategy_name=vote.strategy_name,
            regime_name=regime.regime.value,
            segment=segment,
        )
        checks = dict(base_decision.checks)
        checks["ml_segment"] = f"PASS:segment={segment.get('segment_name')}"
        checks["ml_score_raw"] = f"PASS:score={raw_score:.4f}"
        checks["ml_score_calibrated"] = f"PASS:score={calibrated_score:.4f}"
        checks["ml_threshold"] = f"PASS:threshold={threshold:.4f}"
        if calibrated_score < threshold:
            return EntryPolicyDecision.block(
                f"ml: calibrated_score={calibrated_score:.4f}<threshold={threshold:.4f}",
                checks,
            )
        return EntryPolicyDecision.allow(
            f"ml: calibrated_score={calibrated_score:.4f} threshold={threshold:.4f}",
            score=calibrated_score,
            checks=checks,
        )

    def evaluate_shadow(
        self,
        *,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
    ) -> EntryPolicyDecision:
        """Score one synthetic vote without deterministic/base gating."""
        if vote.direction not in (Direction.CE, Direction.PE):
            return EntryPolicyDecision.block(
                "ml_shadow: unsupported_direction",
                {"direction": "BLOCK:unsupported_direction"},
            )

        checks = {
            "volume": "PASS:shadow",
            "momentum": "PASS:shadow",
            "timing": "PASS:shadow",
            "premium": "PASS:shadow",
            "regime": "PASS:shadow",
            "ml_mode": "PASS:shadow_score_all",
        }
        base_decision = EntryPolicyDecision.allow(
            "shadow: base_bypassed",
            score=1.0,
            checks=checks,
        )

        segment = self._resolve_segment(regime.regime.value)
        if segment is None:
            out_checks = dict(base_decision.checks)
            out_checks["ml_segment"] = "BLOCK:no_segment"
            return EntryPolicyDecision.block(
                f"ml_shadow: no segment model for regime={regime.regime.value}",
                out_checks,
            )

        row = build_entry_feature_row(snap=snap, vote=vote, regime=regime, base_decision=base_decision)
        raw_score, calibrated_score = self._score_row(row, segment)
        threshold = self._resolve_threshold(
            strategy_name=vote.strategy_name,
            regime_name=regime.regime.value,
            segment=segment,
        )
        out_checks = dict(base_decision.checks)
        out_checks["ml_segment"] = f"PASS:segment={segment.get('segment_name')}"
        out_checks["ml_score_raw"] = f"PASS:score={raw_score:.4f}"
        out_checks["ml_score_calibrated"] = f"PASS:score={calibrated_score:.4f}"
        out_checks["ml_threshold"] = f"PASS:threshold={threshold:.4f}"
        if calibrated_score < threshold:
            return EntryPolicyDecision.block(
                f"ml_shadow: calibrated_score={calibrated_score:.4f}<threshold={threshold:.4f}",
                out_checks,
            )
        return EntryPolicyDecision.allow(
            f"ml_shadow: calibrated_score={calibrated_score:.4f} threshold={threshold:.4f}",
            score=calibrated_score,
            checks=out_checks,
        )

    def _resolve_segment(self, regime_name: str) -> Optional[dict[str, Any]]:
        segments = self._bundle.get("segments") if isinstance(self._bundle, dict) else None
        if not isinstance(segments, dict):
            return None
        regime_key = str(regime_name or "").strip().upper()
        segment = segments.get(regime_key)
        if segment is not None:
            return segment
        # Global segmentation stores one shared model under GLOBAL.
        # Fallback avoids blocking all trades when runtime regime names
        # (TRENDING/SIDEWAYS/PRE_EXPIRY/...) are not explicit segment keys.
        return segments.get("GLOBAL")

    def _score_row(self, row: dict[str, Any], segment: dict[str, Any]) -> tuple[float, float]:
        feature_columns = list(segment["feature_columns"])
        numeric_columns = list(segment["numeric_columns"])
        categorical_columns = list(segment["categorical_columns"])
        fill_values = dict(segment.get("numeric_fill_values") or {})

        frame = pd.DataFrame([{name: row.get(name) for name in feature_columns}])
        for column in numeric_columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame[column] = frame[column].fillna(fill_values.get(column))
        for column in categorical_columns:
            frame[column] = frame[column].astype("string").fillna("__MISSING__").astype("category")

        model = segment["model"]
        raw_score = float(model.predict_proba(frame)[:, 1][0])
        calibrated_score = self._apply_calibrator(
            raw_score=raw_score,
            method=str(segment.get("calibration_method") or "none"),
            calibrator=segment.get("calibrator"),
        )
        return raw_score, calibrated_score

    def _resolve_threshold(self, *, strategy_name: str, regime_name: str, segment: dict[str, Any]) -> float:
        strategy_key = str(strategy_name or "").strip().upper()
        regime_key = str(regime_name or "").strip().upper()
        exact = self._strategy_regime_threshold_overrides.get((regime_key, strategy_key))
        if exact is not None:
            return float(exact)
        strategy_override = self._strategy_threshold_overrides.get(strategy_key)
        if strategy_override is not None:
            return float(strategy_override)
        if self._default_threshold is not None:
            return float(self._default_threshold)
        return float(segment.get("threshold") or 0.60)

    @staticmethod
    def _apply_calibrator(*, raw_score: float, method: str, calibrator: object) -> float:
        mode = str(method or "none").strip().lower()
        if calibrator is None or mode == "none":
            return raw_score
        if mode == "platt":
            return float(calibrator.predict_proba([[raw_score]])[:, 1][0])
        if mode == "isotonic":
            value = calibrator.predict([raw_score])
            return float(value[0])
        return raw_score

    @staticmethod
    def _threshold_kwargs_for_policy(policy_id: str) -> dict[str, Any]:
        policy_key = str(policy_id or "").strip()
        custom_fixed = _parse_custom_fixed_threshold_policy(policy_key)
        if custom_fixed is not None:
            return {"default_threshold": float(custom_fixed)}
        if policy_key == THRESHOLD_FIXED_060:
            return {"default_threshold": 0.60}
        if policy_key == THRESHOLD_FIXED_065:
            return {"default_threshold": 0.65}
        if policy_key == THRESHOLD_SEGMENT_OPTIMAL:
            return {}
        if policy_key == THRESHOLD_STRATEGY_OVERRIDE:
            return {
                "strategy_threshold_overrides": dict(STRATEGY_OVERRIDE_THRESHOLDS),
                "strategy_regime_threshold_overrides": dict(STRATEGY_REGIME_OVERRIDE_THRESHOLDS),
            }
        raise KeyError(f"unknown threshold policy: {policy_key}")


def _normalize_registry_bundle_path(row: pd.Series) -> str:
    bundle_path = str(row.get("bundle_path") or "").strip()
    if bundle_path:
        return bundle_path

    summary_json = str(row.get("summary_json") or "").strip()
    if not summary_json:
        raise ValueError("registry row must include either bundle_path or summary_json")
    summary_path = Path(summary_json)
    if not summary_path.exists():
        raise FileNotFoundError(f"summary_json does not exist: {summary_path}")

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_bundle_path = str(payload.get("bundle_path") or "").strip()
    if not summary_bundle_path:
        raise ValueError(f"summary_json missing bundle_path: {summary_path}")
    return summary_bundle_path
