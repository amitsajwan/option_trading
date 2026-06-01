from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

from snapshot_app.core.stage_views import project_stage_views, project_stage_views_v2

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
    risk_basis: str = "option_premium"
    model_diagnostics: dict[str, Any] = field(default_factory=dict)
    # When the predictor pre-selects the strike (option-P&L bundle path), set
    # `selected_strike` so PureMLEngine.evaluate skips smart-strike entirely.
    # This guarantees labeler-runtime equivalence: the labeler used the same
    # strike-pick rule (snapshot.atm_strike + offset_steps * strike_step), so
    # the runtime MUST use the same one — smart-strike's confidence-based
    # OTM_1 shift was the source of the per-trade edge gap observed in the
    # 2024-08/09 holdout validation.
    selected_strike: Optional[int] = None
    selected_strike_reason: Optional[str] = None


@dataclass(frozen=True)
class PureMLRuntimeControls:
    block_expiry: bool = False
    bypass_deterministic_gates: bool = False


def is_staged_runtime_bundle(bundle: dict[str, object]) -> bool:
    return str(bundle.get("kind") or "").strip() == STAGED_RUNTIME_BUNDLE_KIND


def load_staged_policy(path: str) -> dict[str, Any]:
    from ..utils.gcs_artifact import resolve_artifact_path
    return load_staged_runtime_policy(resolve_artifact_path(str(path)))


def load_staged_model_package(path: str | Path) -> dict[str, object]:
    from ..utils.gcs_artifact import resolve_artifact_path
    resolved = resolve_artifact_path(str(path))
    package = joblib.load(Path(resolved))
    if not isinstance(package, dict):
        raise ValueError("pure ml model package must be dict")
    if not is_staged_runtime_bundle(package):
        raise ValueError("ml_pure requires a staged runtime bundle")
    return package


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


def _inject_runtime_snapshot_fields(feature_row: dict[str, object], snap: Any) -> dict[str, object]:
    out = dict(feature_row)
    ts = snap.timestamp_or_now
    if _is_missing_value(out.get("trade_date")):
        trade_date = str(getattr(snap, "trade_date", "") or "").strip()
        out["trade_date"] = trade_date or ts.date().isoformat()
    if _is_missing_value(out.get("year")):
        out["year"] = int(ts.year)
    if _is_missing_value(out.get("timestamp")):
        out["timestamp"] = ts.isoformat()
    if _is_missing_value(out.get("snapshot_id")):
        snapshot_id = str(getattr(snap, "snapshot_id", "") or "").strip()
        if snapshot_id:
            out["snapshot_id"] = snapshot_id
    raw_payload = getattr(snap, "raw_payload", {}) if hasattr(snap, "raw_payload") else {}
    if _is_missing_value(out.get("instrument")) and isinstance(raw_payload, dict):
        instrument = str(raw_payload.get("instrument") or "").strip().upper()
        if instrument:
            out["instrument"] = instrument
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


def _normalise_feature_value(value: object) -> object:
    if value is None:
        return None
    try:
        f = float(value)
    except Exception:
        return str(value)
    if not np.isfinite(f):
        return None
    return round(float(f), 10)


def _model_input_diagnostics(feature_row: dict[str, object], package: dict[str, Any]) -> dict[str, Any]:
    feature_cols = [str(col) for col in list(package.get("feature_columns") or [])]
    values: list[tuple[str, object]] = []
    missing: list[str] = []
    non_null = 0
    for col in feature_cols:
        if col not in feature_row:
            missing.append(col)
            values.append((col, None))
            continue
        value = _normalise_feature_value(feature_row.get(col))
        if value is not None:
            non_null += 1
        values.append((col, value))
    payload = json.dumps(values, sort_keys=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    out: dict[str, Any] = {
        "feature_count": len(feature_cols),
        "non_null_count": int(non_null),
        "missing_count": int(len(missing)),
        "input_hash": digest[:16],
        "input_hash32": int(digest[:8], 16),
    }
    if missing:
        out["missing_features"] = missing[:20]
    if str(os.getenv("STRATEGY_ML_MODEL_IO_VERBOSE", "")).strip().lower() in {"1", "true", "yes", "on"}:
        out["features"] = {name: value for name, value in values}
    return out


def _record_stage_output(
    diagnostics: dict[str, Any],
    stage: str,
    *,
    output_col: str,
    output_prob: float,
    reason: Optional[str] = None,
) -> None:
    stage_diag = diagnostics.setdefault(stage, {})
    stage_diag["output_col"] = str(output_col)
    stage_diag["output_prob"] = float(output_prob)
    if reason:
        stage_diag["reason"] = str(reason)
    if str(os.getenv("STRATEGY_ML_MODEL_IO_LOG", "")).strip().lower() in {"1", "true", "yes", "on"}:
        logger.info(
            "ml_model_io stage=%s input_hash=%s features=%s non_null=%s missing=%s output_col=%s output_prob=%.10f reason=%s",
            stage,
            stage_diag.get("input_hash"),
            stage_diag.get("feature_count"),
            stage_diag.get("non_null_count"),
            stage_diag.get("missing_count"),
            output_col,
            float(output_prob),
            reason or "",
        )


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


def _is_v2_bundle(bundle: dict[str, Any]) -> bool:
    """Return True when the staged bundle uses the supported V2 training contract.

    Preferred detection uses explicit runtime metadata written at publish time:
    ``runtime.view_version``, ``runtime.support_dataset``, and ``runtime.view_ids``.
    Older bundles fall back to stage1 feature inspection for backward compatibility.
    """
    runtime = bundle.get("runtime") or {}
    view_version = str(runtime.get("view_version") or "").strip().lower()
    if view_version in {"v1", "v2"}:
        return view_version == "v2"
    support_dataset = str(runtime.get("support_dataset") or "").strip().lower()
    if support_dataset:
        return support_dataset == "snapshots_ml_flat_v2"
    view_ids = dict(runtime.get("view_ids") or {})
    stage_view_ids = [str(view_ids.get(stage) or "").strip().lower() for stage in ("stage1", "stage2", "stage3")]
    if stage_view_ids and all(view_id.endswith("_v2") for view_id in stage_view_ids if view_id):
        return True
    if stage_view_ids and all(view_id.endswith("_v1") for view_id in stage_view_ids if view_id):
        return False
    stage1_cols = list((((bundle.get("stages") or {}).get("stage1") or {}).get("model_package") or {}).get("feature_columns") or [])
    return any(str(c).startswith(("vel_", "ctx_am_", "ctx_gap_")) for c in stage1_cols)


def predict_staged(
    *,
    engine: Any,
    snap: Any,
    rolling_features: dict[str, object],
    bundle: dict[str, Any],
    policy: dict[str, Any],
) -> StagedRuntimeDecision:
    gate_ids = list(((bundle.get("runtime") or {}).get("prefilter_gate_ids") or (policy.get("runtime") or {}).get("prefilter_gate_ids") or []))
    bypass_deterministic_gates = bool(getattr(engine._runtime_controls, "bypass_deterministic_gates", False))
    use_v2 = _is_v2_bundle(bundle)
    if use_v2:
        raw_views = project_stage_views_v2(snap.raw_payload)
        stage_views = {
            "stage1_entry_view": raw_views["stage1_entry_view_v2"],
            "stage2_direction_view": raw_views["stage2_direction_view_v2"],
            "stage3_recipe_view": raw_views["stage3_recipe_view_v2"],
        }
    else:
        stage_views = project_stage_views(snap.raw_payload)
    stage1_package = dict(((bundle.get("stages") or {}).get("stage1") or {}).get("model_package") or {})
    stage2_package = dict(((bundle.get("stages") or {}).get("stage2") or {}).get("model_package") or {})
    stage3_packages = dict(((bundle.get("stages") or {}).get("stage3") or {}).get("recipe_packages") or {})
    recipe_catalog = list(policy.get("recipe_catalog") or [])

    stage1_row = _inject_runtime_snapshot_fields(
        _backfill_stage_row(engine._merge_feature_rows(stage_views["stage1_entry_view"], rolling_features), rolling_features),
        snap,
    )
    diagnostics: dict[str, Any] = {
        "stage1": _model_input_diagnostics(stage1_row, stage1_package),
    }
    if not bypass_deterministic_gates:
        gate_reason, _ = _run_prefilter_chain(engine, snap, stage1_row, gate_ids)
        if gate_reason is not None:
            diagnostics["stage1"]["reason"] = str(gate_reason)
            return StagedRuntimeDecision(action="HOLD", reason=gate_reason, model_diagnostics=diagnostics)
    if "feature_completeness_v1" in gate_ids:
        incomplete_reason = _feature_completeness_reason(stage1_row, stage1_package, max_nan_features=engine._max_nan_features)
        if incomplete_reason is not None:
            diagnostics["stage1"]["reason"] = str(incomplete_reason)
            return StagedRuntimeDecision(action="HOLD", reason=incomplete_reason, model_diagnostics=diagnostics)

    entry_prob = _score_prob(stage1_package, stage1_row, default_prob_col="move_prob")
    _record_stage_output(diagnostics, "stage1", output_col="entry_prob", output_prob=float(entry_prob))
    entry_threshold = float(dict(policy["stage1"])["selected_threshold"])
    if (not bypass_deterministic_gates) and float(entry_prob) < entry_threshold:
        diagnostics["stage1"]["reason"] = "entry_below_threshold"
        return StagedRuntimeDecision(action="HOLD", reason="entry_below_threshold", entry_prob=float(entry_prob), model_diagnostics=diagnostics)

    stage2_row = _inject_runtime_snapshot_fields(
        _backfill_stage_row(engine._merge_feature_rows(stage_views["stage2_direction_view"], rolling_features), rolling_features),
        snap,
    )
    diagnostics["stage2"] = _model_input_diagnostics(stage2_row, stage2_package)
    if "feature_completeness_v1" in gate_ids:
        incomplete_reason = _feature_completeness_reason(stage2_row, stage2_package, max_nan_features=engine._max_nan_features)
        if incomplete_reason is not None:
            diagnostics["stage2"]["reason"] = "stage2_feature_incomplete"
            return StagedRuntimeDecision(action="HOLD", reason="stage2_feature_incomplete", entry_prob=float(entry_prob), model_diagnostics=diagnostics)
    direction_up_prob = _score_prob(stage2_package, stage2_row, default_prob_col="direction_up_prob")
    _record_stage_output(diagnostics, "stage2", output_col="direction_up_prob", output_prob=float(direction_up_prob))
    ce_prob = float(direction_up_prob)
    pe_prob = float(1.0 - direction_up_prob)
    stage2_policy = dict(policy["stage2"])
    ce_threshold = float(stage2_policy["selected_ce_threshold"])
    pe_threshold = float(stage2_policy["selected_pe_threshold"])
    min_edge = float(stage2_policy["selected_min_edge"])
    ce_ok = ce_prob >= ce_threshold
    pe_ok = pe_prob >= pe_threshold
    if bypass_deterministic_gates:
        direction = "CE" if ce_prob >= pe_prob else "PE"
    elif ce_ok and pe_ok:
        if abs(ce_prob - pe_prob) < min_edge:
            diagnostics["stage2"]["reason"] = "direction_low_edge_conflict"
            return StagedRuntimeDecision(action="HOLD", reason="direction_low_edge_conflict", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)
        direction = "CE" if ce_prob >= pe_prob else "PE"
    elif ce_ok:
        direction = "CE"
    elif pe_ok:
        direction = "PE"
    else:
        diagnostics["stage2"]["reason"] = "direction_below_threshold"
        return StagedRuntimeDecision(action="HOLD", reason="direction_below_threshold", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)

    strike = snap.atm_strike
    if strike is None or int(strike) <= 0:
        return StagedRuntimeDecision(action="HOLD", reason="missing_atm_strike", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)
    if (not bypass_deterministic_gates) and "liquidity_gate_v1" in gate_ids and not engine._liquidity_ok(snap=snap, direction=direction, strike=int(strike)):
        return StagedRuntimeDecision(action="HOLD", reason="liquidity_gate_block", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)

    stage3_row = _inject_runtime_snapshot_fields(
        _backfill_stage_row(engine._merge_feature_rows(stage_views["stage3_recipe_view"], rolling_features), rolling_features),
        snap,
    )
    stage3_row["stage1_entry_prob"] = float(entry_prob)
    stage3_row["stage2_direction_up_prob"] = float(direction_up_prob)
    stage3_row["stage2_direction_down_prob"] = float(1.0 - direction_up_prob)
    stage3_diag_package = next(iter(stage3_packages.values()), {}) if stage3_packages else {}
    diagnostics["stage3"] = _model_input_diagnostics(stage3_row, dict(stage3_diag_package))
    diagnostics["stage3"]["recipe_outputs"] = {}
    recipe_scores: list[tuple[str, float]] = []
    for recipe_id, package in sorted(stage3_packages.items()):
        if "feature_completeness_v1" in gate_ids:
            incomplete_reason = _feature_completeness_reason(stage3_row, dict(package), max_nan_features=engine._max_nan_features)
            if incomplete_reason is not None:
                diagnostics["stage3"]["reason"] = "stage3_feature_incomplete"
                return StagedRuntimeDecision(action="HOLD", reason="stage3_feature_incomplete", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)
        recipe_prob = _score_prob(dict(package), stage3_row, default_prob_col="move_prob")
        diagnostics["stage3"]["recipe_outputs"][str(recipe_id)] = float(recipe_prob)
        recipe_scores.append((str(recipe_id), recipe_prob))
    recipe_scores.sort(key=lambda item: (-float(item[1]), str(item[0])))
    if not recipe_scores:
        diagnostics["stage3"]["reason"] = "recipe_scores_missing"
        return StagedRuntimeDecision(action="HOLD", reason="recipe_scores_missing", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, model_diagnostics=diagnostics)
    top_recipe, top_prob = recipe_scores[0]
    second_prob = recipe_scores[1][1] if len(recipe_scores) > 1 else 0.0
    _record_stage_output(diagnostics, "stage3", output_col=str(top_recipe), output_prob=float(top_prob))
    stage3_policy = dict(policy["stage3"])
    recipe_threshold = float(stage3_policy["selected_threshold"])
    recipe_margin_min = float(stage3_policy["selected_margin_min"])
    logger.warning(
        f"[RECIPE_SELECTION] top={top_recipe} prob={top_prob:.4f} margin={top_prob - second_prob:.4f} "
        f"threshold={recipe_threshold:.4f} margin_min={recipe_margin_min:.4f} "
        f"all_scores={[(r, round(p, 4)) for r, p in recipe_scores]}"
    )
    if (not bypass_deterministic_gates) and float(top_prob) < recipe_threshold:
        diagnostics["stage3"]["reason"] = "recipe_below_threshold"
        return StagedRuntimeDecision(action="HOLD", reason="recipe_below_threshold", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, recipe_id=str(top_recipe), recipe_prob=float(top_prob), model_diagnostics=diagnostics)
    if (not bypass_deterministic_gates) and float(top_prob - second_prob) < recipe_margin_min:
        diagnostics["stage3"]["reason"] = "recipe_low_margin"
        return StagedRuntimeDecision(action="HOLD", reason="recipe_low_margin", entry_prob=float(entry_prob), direction_up_prob=float(direction_up_prob), ce_prob=ce_prob, pe_prob=pe_prob, recipe_id=str(top_recipe), recipe_prob=float(top_prob), recipe_margin=float(top_prob - second_prob), model_diagnostics=diagnostics)

    recipe_meta = next((dict(item) for item in recipe_catalog if str(item.get("recipe_id") or "") == str(top_recipe)), {})
    raw_stop = float(recipe_meta.get("stop_loss_pct") or 0.0)
    raw_target = float(recipe_meta.get("take_profit_pct") or 0.0)
    risk_basis = str(recipe_meta.get("risk_basis") or "option_premium").strip().lower()
    if risk_basis not in ("underlying", "option_premium"):
        risk_basis = "option_premium"
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
        stop_loss_pct=raw_stop if raw_stop > 0 else None,
        target_pct=raw_target if raw_target > 0 else None,
        risk_basis=risk_basis,
        model_diagnostics=diagnostics,
    )
