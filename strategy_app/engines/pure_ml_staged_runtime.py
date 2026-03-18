from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

from snapshot_app.core.stage_views import project_stage_views

from ml_pipeline_2.inference_contract.predict import predict_probabilities_from_frame
from ml_pipeline_2.staged.runtime_contract import STAGED_RUNTIME_BUNDLE_KIND, load_staged_runtime_policy

_STAGED_ROLLING_ALIASES: dict[str, str] = {
    "days_to_expiry": "dte_days",
    "fut_return_1m": "ret_1m",
    "fut_return_3m": "ret_3m",
    "fut_return_5m": "ret_5m",
    "rsi_14_1m": "rsi_14",
    "price_vs_vwap": "vwap_distance",
    "dist_from_day_high": "distance_from_day_high",
    "dist_from_day_low": "distance_from_day_low",
    "orh_broken": "opening_range_breakout_up",
    "orl_broken": "opening_range_breakout_down",
    "pcr": "pcr_oi",
    "atm_ce_return_1m": "atm_call_return_1m",
    "atm_pe_return_1m": "atm_put_return_1m",
}


@dataclass(frozen=True)
class StagedRuntimeDecision:
    action: str
    reason: str
    entry_prob: float = 0.0
    direction_up_prob: float = 0.0
    ce_prob: float = 0.0
    pe_prob: float = 0.0
    recipe_id: Optional[str] = None
    recipe_prob: float = 0.0
    recipe_margin: float = 0.0
    horizon_minutes: Optional[int] = None
    stop_loss_pct: Optional[float] = None
    target_pct: Optional[float] = None


def is_staged_runtime_bundle(bundle: dict[str, object]) -> bool:
    return str(bundle.get("kind") or "").strip() == STAGED_RUNTIME_BUNDLE_KIND


def load_staged_policy(path: str) -> dict[str, Any]:
    return load_staged_runtime_policy(path)


def _feature_completeness_reason(feature_row: dict[str, object], package: dict[str, Any], *, max_nan_features: int) -> Optional[str]:
    required = [str(col) for col in list(package.get("feature_columns") or [])]
    nan_count = 0
    for col in required:
        value = feature_row.get(col, np.nan)
        try:
            out = float(value)
        except Exception:
            nan_count += 1
            continue
        if not np.isfinite(out):
            nan_count += 1
    if nan_count > int(max_nan_features):
        return "feature_incomplete"
    return None


def _is_missing_value(value: object) -> bool:
    if value is None:
        return True
    try:
        return not np.isfinite(float(value))
    except Exception:
        return False


def _backfill_stage_row(view_row: dict[str, object], rolling_features: dict[str, object]) -> dict[str, object]:
    if not rolling_features:
        return dict(view_row)
    out = dict(view_row)
    for view_key, rolling_key in _STAGED_ROLLING_ALIASES.items():
        if not _is_missing_value(out.get(view_key)):
            continue
        rolling_value = rolling_features.get(rolling_key)
        if _is_missing_value(rolling_value):
            continue
        out[view_key] = rolling_value
    return out


def _score_prob(package: dict[str, Any], feature_row: dict[str, object], *, default_prob_col: str) -> float:
    feature_frame = pd.DataFrame([feature_row])
    # The "error" policy here only guards against structurally absent columns.
    # NaN content is checked separately by _feature_completeness_reason before scoring.
    probs, _ = predict_probabilities_from_frame(
        feature_frame,
        package,
        missing_policy_override="error",
        context=default_prob_col,
    )
    if default_prob_col in probs.columns:
        return float(pd.to_numeric(probs[default_prob_col], errors="coerce").iloc[0])
    first_col = str(probs.columns[0])
    return float(pd.to_numeric(probs[first_col], errors="coerce").iloc[0])


def _run_prefilter_chain(engine: Any, snap: Any, stage1_row: dict[str, object], gate_ids: list[str]) -> tuple[Optional[str], Any]:
    regime_signal = None
    for gate_id in gate_ids:
        if gate_id == "rollout_guard_v1":
            # Reserved for future percentage-rollout control; intentional no-op for now.
            continue
        if gate_id == "risk_halt_pause_v1":
            if engine._risk.is_halted:
                return "risk_halt", None
            if engine._risk.is_paused:
                return "risk_pause", None
        elif gate_id == "valid_entry_phase_v1":
            if not snap.is_valid_entry_phase:
                return "invalid_entry_phase", None
        elif gate_id == "startup_warmup_v1":
            warmup_blocked, _ = engine._entry_warmup_status()
            if warmup_blocked:
                return "entry_warmup_block", None
        elif gate_id == "feature_freshness_v1":
            stale_reason = engine._check_feature_freshness(snap)
            if stale_reason is not None:
                return stale_reason, None
        elif gate_id == "regime_gate_v1":
            regime_signal = regime_signal or engine._regime.classify(snap)
            if engine._runtime_controls.block_expiry and regime_signal.regime.value == "EXPIRY":
                return "regime_expiry", regime_signal
            if regime_signal.regime.value in {"SIDEWAYS", "AVOID"}:
                return f"regime_{regime_signal.regime.value.lower()}", regime_signal
        elif gate_id == "regime_confidence_gate_v1":
            regime_signal = regime_signal or engine._regime.classify(snap)
            if engine._runtime_controls.block_expiry and regime_signal.regime.value == "EXPIRY":
                return "regime_expiry", regime_signal
            if float(regime_signal.confidence) < 0.60:
                return "regime_low_confidence", regime_signal
        elif gate_id == "feature_completeness_v1":
            continue
        elif gate_id == "liquidity_gate_v1":
            continue
        else:
            raise ValueError(f"unknown staged runtime gate_id: {gate_id}")
    if engine._runtime_controls.block_expiry:
        regime_signal = regime_signal or engine._regime.classify(snap)
        if regime_signal.regime.value == "EXPIRY":
            return "regime_expiry", regime_signal
    return None, regime_signal


def predict_staged(
    *,
    engine: Any,
    snap: Any,
    rolling_features: dict[str, object],
    bundle: dict[str, Any],
    policy: dict[str, Any],
) -> StagedRuntimeDecision:
    gate_ids = list(((bundle.get("runtime") or {}).get("prefilter_gate_ids") or (policy.get("runtime") or {}).get("prefilter_gate_ids") or []))
    stage_views = project_stage_views(snap.raw_payload)
    stage1_package = dict(((bundle.get("stages") or {}).get("stage1") or {}).get("model_package") or {})
    stage2_package = dict(((bundle.get("stages") or {}).get("stage2") or {}).get("model_package") or {})
    stage3_packages = dict(((bundle.get("stages") or {}).get("stage3") or {}).get("recipe_packages") or {})
    recipe_catalog = list(policy.get("recipe_catalog") or [])

    stage1_row = _backfill_stage_row(engine._merge_feature_rows(stage_views["stage1_entry_view"], rolling_features), rolling_features)
    gate_reason, _ = _run_prefilter_chain(engine, snap, stage1_row, gate_ids)
    if gate_reason is not None:
        return StagedRuntimeDecision(action="HOLD", reason=gate_reason)
    if "feature_completeness_v1" in gate_ids:
        incomplete_reason = _feature_completeness_reason(stage1_row, stage1_package, max_nan_features=engine._max_nan_features)
        if incomplete_reason is not None:
            return StagedRuntimeDecision(action="HOLD", reason=incomplete_reason)

    entry_prob = _score_prob(stage1_package, stage1_row, default_prob_col="move_prob")
    entry_threshold = float(dict(policy["stage1"])["selected_threshold"])
    if float(entry_prob) < entry_threshold:
        return StagedRuntimeDecision(action="HOLD", reason="entry_below_threshold", entry_prob=float(entry_prob))

    stage2_row = _backfill_stage_row(engine._merge_feature_rows(stage_views["stage2_direction_view"], rolling_features), rolling_features)
    if "feature_completeness_v1" in gate_ids:
        incomplete_reason = _feature_completeness_reason(stage2_row, stage2_package, max_nan_features=engine._max_nan_features)
        if incomplete_reason is not None:
            return StagedRuntimeDecision(action="HOLD", reason="stage2_feature_incomplete", entry_prob=float(entry_prob))
    direction_up_prob = _score_prob(stage2_package, stage2_row, default_prob_col="direction_up_prob")
    ce_prob = float(direction_up_prob)
    pe_prob = float(1.0 - direction_up_prob)
    stage2_policy = dict(policy["stage2"])
    ce_threshold = float(stage2_policy["selected_ce_threshold"])
    pe_threshold = float(stage2_policy["selected_pe_threshold"])
    min_edge = float(stage2_policy["selected_min_edge"])
    ce_ok = ce_prob >= ce_threshold
    pe_ok = pe_prob >= pe_threshold
    if ce_ok and pe_ok:
        if abs(ce_prob - pe_prob) < min_edge:
            return StagedRuntimeDecision(action="HOLD", reason="direction_low_edge_conflict", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)
        direction = "CE" if ce_prob >= pe_prob else "PE"
    elif ce_ok:
        direction = "CE"
    elif pe_ok:
        direction = "PE"
    else:
        return StagedRuntimeDecision(action="HOLD", reason="direction_below_threshold", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)

    strike = snap.atm_strike
    if strike is None or int(strike) <= 0:
        return StagedRuntimeDecision(action="HOLD", reason="missing_atm_strike", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)
    if "liquidity_gate_v1" in gate_ids and not engine._liquidity_ok(snap=snap, direction=direction, strike=int(strike)):
        return StagedRuntimeDecision(action="HOLD", reason="liquidity_gate_block", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)

    stage3_row = _backfill_stage_row(engine._merge_feature_rows(stage_views["stage3_recipe_view"], rolling_features), rolling_features)
    stage3_row["stage1_entry_prob"] = float(entry_prob)
    stage3_row["stage2_direction_up_prob"] = float(direction_up_prob)
    stage3_row["stage2_direction_down_prob"] = float(1.0 - direction_up_prob)
    recipe_scores: list[tuple[str, float]] = []
    for recipe_id, package in sorted(stage3_packages.items()):
        if "feature_completeness_v1" in gate_ids:
            incomplete_reason = _feature_completeness_reason(stage3_row, dict(package), max_nan_features=engine._max_nan_features)
            if incomplete_reason is not None:
                return StagedRuntimeDecision(action="HOLD", reason="stage3_feature_incomplete", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)
        recipe_scores.append((str(recipe_id), _score_prob(dict(package), stage3_row, default_prob_col="move_prob")))
    recipe_scores.sort(key=lambda item: (-float(item[1]), str(item[0])))
    if not recipe_scores:
        return StagedRuntimeDecision(action="HOLD", reason="recipe_scores_missing", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob)
    top_recipe, top_prob = recipe_scores[0]
    second_prob = recipe_scores[1][1] if len(recipe_scores) > 1 else 0.0
    stage3_policy = dict(policy["stage3"])
    recipe_threshold = float(stage3_policy["selected_threshold"])
    recipe_margin_min = float(stage3_policy["selected_margin_min"])
    if float(top_prob) < recipe_threshold:
        return StagedRuntimeDecision(action="HOLD", reason="recipe_below_threshold", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, recipe_id=str(top_recipe), recipe_prob=float(top_prob))
    if float(top_prob - second_prob) < recipe_margin_min:
        return StagedRuntimeDecision(action="HOLD", reason="recipe_low_margin", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, recipe_id=str(top_recipe), recipe_prob=float(top_prob), recipe_margin=float(top_prob - second_prob))

    recipe_meta = next((dict(item) for item in recipe_catalog if str(item.get("recipe_id") or "") == str(top_recipe)), {})
    return StagedRuntimeDecision(
        action=("BUY_CE" if direction == "CE" else "BUY_PE"),
        reason="staged_entry_ready",
        entry_prob=float(entry_prob),
        direction_up_prob=float(direction_up_prob),
        ce_prob=float(ce_prob),
        pe_prob=float(pe_prob),
        recipe_id=str(top_recipe),
        recipe_prob=float(top_prob),
        recipe_margin=float(top_prob - second_prob),
        horizon_minutes=int(recipe_meta.get("horizon_minutes")) if recipe_meta else None,
        stop_loss_pct=float(recipe_meta.get("stop_loss_pct")) if recipe_meta else None,
        target_pct=float(recipe_meta.get("take_profit_pct")) if recipe_meta else None,
    )
