"""Runtime predictor for option-P&L bundles produced by
ml_pipeline_2/scripts/publish_option_pnl_model.py.

Bundle contents (per the publish script):
    model.joblib          — XGBClassifier
    feature_columns.json  — ordered list of feature names
    metadata.json         — recipe params, decision threshold, model params

This module exposes three public entry points:
    build_decision_from_bundle(bundle, snap)  — single bundle (used by select_best_bundle_decision)
    load_bundles_from_env()                   — load all bundles from OPTION_PNL_MODEL_BUNDLE (comma-sep)
    select_best_bundle_decision(bundles, snap) — score all bundles, return highest-margin ENTRY or HOLD

Why fake a StagedRuntimeDecision rather than a separate path: PureMLEngine's
evaluate() consumes the staged decision object and handles the rest
(smart-strike → premium check → TradeSignal build → position tracker).
By constructing a compatible decision, we reuse all that hardened code.

Key fields for our case (PE recipe):
    action: "BUY_PE" if prob >= threshold else "HOLD"
    recipe_id: e.g. "ATM_PE_15" (from metadata)
    entry_prob: predicted probability
    pe_prob: 1.0 (we always go PE for PE recipes)
    ce_prob: 0.0
    recipe_prob: predicted probability (alias for downstream UI)
    risk_basis: "option_premium" (premium-relative stops)
    stop_loss_pct: from recipe_params (e.g. 0.25 = 25% of premium)
    target_pct: from recipe_params (e.g. 0.40 = 40% of premium)
    horizon_minutes: from recipe_params.max_hold_bars

Equivalence with labeler:
    - We use snap.atm_strike directly (same as labeler) — no recompute
    - For CE/PE direction, recipe.option_type is fixed per bundle
    - Stop/target are % of premium (matches labeler's stop_pct_of_premium)
    - Strike offset (for OTM/ITM recipes) computed against snap.strike_step()
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np

from .pure_ml_staged_runtime import StagedRuntimeDecision


@dataclass(frozen=True)
class OptionPnlBundle:
    """In-memory representation of a published option-P&L model bundle."""
    run_id: str
    recipe_id: str
    option_type: str          # "CE" | "PE"
    strike_offset_steps: int
    max_hold_bars: int
    stop_pct_of_premium: float
    target_pct_of_premium: float
    decision_threshold: float
    feature_columns: list[str]
    model: Any                # XGBClassifier
    metadata: dict[str, Any]


def load_option_pnl_bundle(bundle_dir: str | Path) -> OptionPnlBundle:
    """Load a published option-P&L model bundle from disk.

    Raises FileNotFoundError if any required file is missing. Raises
    ValueError if metadata fields don't match expected schema (catches
    bundle-version mismatches before runtime can use a broken bundle).
    """
    root = Path(bundle_dir)
    if not root.exists():
        raise FileNotFoundError(f"option-pnl bundle dir missing: {root}")

    metadata_path = root / "metadata.json"
    feature_cols_path = root / "feature_columns.json"
    model_path = root / "model.joblib"
    for p in [metadata_path, feature_cols_path, model_path]:
        if not p.exists():
            raise FileNotFoundError(f"bundle missing required file: {p}")

    metadata = json.loads(metadata_path.read_text())
    if str(metadata.get("bundle_version") or "") != "option_pnl_v1":
        raise ValueError(
            f"unsupported bundle_version: {metadata.get('bundle_version')!r} "
            f"(expected 'option_pnl_v1')"
        )

    recipe_params = metadata.get("recipe_params") or {}
    required_recipe_keys = ("option_type", "strike_offset_steps", "max_hold_bars",
                            "stop_pct_of_premium", "target_pct_of_premium")
    for k in required_recipe_keys:
        if k not in recipe_params:
            raise ValueError(f"bundle metadata.recipe_params missing key: {k!r}")
    if str(recipe_params["option_type"]).upper() not in ("CE", "PE"):
        raise ValueError(f"recipe option_type must be CE or PE, got {recipe_params['option_type']!r}")

    feat_cols_payload = json.loads(feature_cols_path.read_text())
    feat_cols = list(feat_cols_payload.get("feature_columns") or [])
    if not feat_cols:
        raise ValueError("bundle feature_columns.json has empty feature_columns")

    model = joblib.load(model_path)

    threshold_raw = metadata.get("decision_threshold")
    threshold = 0.5 if threshold_raw is None else float(threshold_raw)
    return OptionPnlBundle(
        run_id=str(metadata.get("run_id") or root.name),
        recipe_id=str(metadata.get("recipe_id") or ""),
        option_type=str(recipe_params["option_type"]).upper(),
        strike_offset_steps=int(recipe_params["strike_offset_steps"]),
        max_hold_bars=int(recipe_params["max_hold_bars"]),
        stop_pct_of_premium=float(recipe_params["stop_pct_of_premium"]),
        target_pct_of_premium=float(recipe_params["target_pct_of_premium"]),
        decision_threshold=threshold,
        feature_columns=feat_cols,
        model=model,
        metadata=metadata,
    )


def load_bundle_from_env() -> Optional[OptionPnlBundle]:
    """Read OPTION_PNL_MODEL_BUNDLE env var and load the first path. Returns None if unset.

    Kept for backward compatibility. Prefer load_bundles_from_env() for
    multi-bundle support."""
    bundles = load_bundles_from_env()
    return bundles[0] if bundles else None


def load_bundles_from_env() -> list[OptionPnlBundle]:
    """Read OPTION_PNL_MODEL_BUNDLE env var and load all bundles.

    Accepts a comma-separated list of bundle directory paths, e.g.:
        OPTION_PNL_MODEL_BUNDLE=/path/to/pe_bundle,/path/to/ce_bundle

    Returns a list of loaded bundles (may be empty). Errors on individual
    paths are logged and skipped so one bad path cannot block others.
    """
    env_val = os.getenv("OPTION_PNL_MODEL_BUNDLE", "")
    if not env_val or not env_val.strip():
        return []
    paths = [p.strip() for p in env_val.split(",") if p.strip()]
    bundles: list[OptionPnlBundle] = []
    for path in paths:
        try:
            bundles.append(load_option_pnl_bundle(path))
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "OPTION_PNL_MODEL_BUNDLE: failed to load bundle at %r — skipping: %s", path, exc
            )
    return bundles


def select_best_bundle_decision(
    bundles: list[OptionPnlBundle],
    snap: Any,
    rolling_features: Optional[dict[str, object]] = None,
) -> "StagedRuntimeDecision":
    """Score all bundles at the current bar and return the highest-confidence decision.

    Selection rule: highest (prob - threshold) margin among bundles that
    clear their threshold. This favours the most confident signal relative
    to each bundle's own calibration point rather than raw probability.

    Returns HOLD if no bundle clears its threshold.
    """
    best_decision = None
    best_margin: float = -1.0
    for bundle in bundles:
        decision = build_decision_from_bundle(
            bundle=bundle, snap=snap, rolling_features=rolling_features
        )
        if decision.action == "HOLD":
            continue
        margin = float(decision.entry_prob) - float(bundle.decision_threshold)
        if margin > best_margin:
            best_margin = margin
            best_decision = decision
    if best_decision is not None:
        from dataclasses import replace  # noqa: PLC0415
        return replace(best_decision, recipe_margin=float(best_margin))
    # All HOLDs — return the last bundle's HOLD for diagnostics
    return build_decision_from_bundle(
        bundle=bundles[-1], snap=snap, rolling_features=rolling_features
    )


def _flatten_snapshot_features(
    snap: Any,
    rolling_features: Optional[dict[str, object]] = None,
) -> dict[str, Any]:
    """Build a snapshots_ml_flat_v2-shaped row from a runtime snapshot.

    The option-P&L model was trained on the flat-v2 dataset. Runtime replay
    receives the nested MarketSnapshot payload, so this function projects the
    subset of flat-v2 names needed by the bundle and overlays the same rolling
    features already maintained by PureMLEngine.
    """
    raw = getattr(snap, "raw_payload", None)
    if not isinstance(raw, dict):
        raw = getattr(snap, "_payload", None) or getattr(snap, "payload", None) or {}
    raw = raw if isinstance(raw, dict) else {}
    rolling = rolling_features if isinstance(rolling_features, dict) else {}
    sc = raw.get("session_context") if isinstance(raw.get("session_context"), dict) else {}
    fb = raw.get("futures_bar") if isinstance(raw.get("futures_bar"), dict) else {}
    fd = raw.get("futures_derived") if isinstance(raw.get("futures_derived"), dict) else {}
    ca = raw.get("chain_aggregates") if isinstance(raw.get("chain_aggregates"), dict) else {}
    atm = raw.get("atm_options") if isinstance(raw.get("atm_options"), dict) else {}
    iv = raw.get("iv_derived") if isinstance(raw.get("iv_derived"), dict) else {}
    vel = raw.get("velocity_enrichment") if isinstance(raw.get("velocity_enrichment"), dict) else {}

    ts = getattr(snap, "timestamp", None)
    ts_now = getattr(snap, "timestamp_or_now", None)
    ts_obj = ts or ts_now

    out: dict[str, Any] = {
        "year": (getattr(ts_obj, "year", None) if ts_obj is not None else raw.get("year")),
        "px_fut_open": _first_present(fb.get("fut_open"), raw.get("px_fut_open")),
        "px_fut_high": _first_present(fb.get("fut_high"), raw.get("px_fut_high")),
        "px_fut_low": _first_present(fb.get("fut_low"), raw.get("px_fut_low")),
        "px_fut_close": _first_present(fb.get("fut_close"), raw.get("px_fut_close")),
        "px_spot_open": _first_present(raw.get("spot_open"), raw.get("px_spot_open")),
        "px_spot_high": _first_present(raw.get("spot_high"), raw.get("px_spot_high")),
        "px_spot_low": _first_present(raw.get("spot_low"), raw.get("px_spot_low")),
        "px_spot_close": _first_present(raw.get("spot_close"), raw.get("px_spot_close")),
        "ret_1m": rolling.get("ret_1m"),
        "ret_3m": rolling.get("ret_3m"),
        "ret_5m": _first_present(rolling.get("ret_5m"), fd.get("fut_return_5m")),
        "ema_9": _first_present(fd.get("ema_9"), rolling.get("ema_9")),
        "ema_21": _first_present(fd.get("ema_21"), rolling.get("ema_21")),
        "ema_50": _first_present(fd.get("ema_50"), rolling.get("ema_50")),
        "ema_9_21_spread": rolling.get("ema_9_21_spread"),
        "ema_9_slope": _first_present(fd.get("ema_9_slope"), rolling.get("ema_9_slope")),
        "ema_21_slope": _first_present(fd.get("ema_21_slope"), rolling.get("ema_21_slope")),
        "ema_50_slope": _first_present(fd.get("ema_50_slope"), rolling.get("ema_50_slope")),
        "osc_rsi_14": rolling.get("osc_rsi_14"),
        "osc_atr_14": rolling.get("atr_14"),
        "osc_atr_ratio": rolling.get("osc_atr_ratio"),
        "osc_atr_percentile": rolling.get("osc_atr_percentile"),
        "osc_atr_daily_percentile": rolling.get("osc_atr_daily_percentile"),
        "vwap_fut": _first_present(fd.get("vwap"), raw.get("vwap_fut")),
        "vwap_distance": _first_present(rolling.get("vwap_distance"), fd.get("price_vs_vwap")),
        "dist_from_day_high": _first_present(rolling.get("distance_from_day_high"), fd.get("dist_from_day_high")),
        "dist_from_day_low": _first_present(rolling.get("distance_from_day_low"), fd.get("dist_from_day_low")),
        "dist_basis": rolling.get("basis"),
        "dist_basis_change_1m": rolling.get("basis_change_1m"),
        "fut_flow_volume": _first_present(fb.get("fut_volume"), raw.get("fut_flow_volume")),
        "fut_flow_oi": _first_present(fb.get("fut_oi"), raw.get("fut_flow_oi")),
        "fut_flow_rel_volume_20": rolling.get("fut_rel_volume_20"),
        "fut_flow_volume_accel_1m": rolling.get("fut_volume_accel_1m"),
        "fut_flow_oi_change_1m": rolling.get("fut_oi_change_1m"),
        "fut_flow_oi_change_5m": rolling.get("fut_oi_change_5m"),
        "fut_flow_oi_rel_20": rolling.get("fut_oi_rel_20"),
        "fut_flow_oi_zscore_20": rolling.get("fut_oi_zscore_20"),
        "opt_flow_rows": _first_present(raw.get("options_rows"), raw.get("strike_count"), getattr(snap, "strike_count", None)),
        "opt_flow_ce_oi_total": _first_present(ca.get("total_ce_oi"), raw.get("opt_flow_ce_oi_total")),
        "opt_flow_pe_oi_total": _first_present(ca.get("total_pe_oi"), raw.get("opt_flow_pe_oi_total")),
        "opt_flow_ce_volume_total": _first_present(raw.get("ce_volume_total"), raw.get("opt_flow_ce_volume_total")),
        "opt_flow_pe_volume_total": _first_present(raw.get("pe_volume_total"), raw.get("opt_flow_pe_volume_total")),
        "opt_flow_pcr_oi": _first_present(rolling.get("opt_flow_pcr_oi"), ca.get("pcr")),
        "pcr_change_5m": rolling.get("pcr_change_5m"),
        "pcr_change_15m": rolling.get("pcr_change_15m"),
        "opt_flow_atm_call_return_1m": rolling.get("atm_call_return_1m"),
        "opt_flow_atm_put_return_1m": rolling.get("atm_put_return_1m"),
        "opt_flow_atm_oi_change_1m": rolling.get("atm_oi_change_1m"),
        "atm_oi_ratio": _first_present(rolling.get("atm_oi_ratio"), atm.get("atm_oi_ratio")),
        "near_atm_oi_ratio": rolling.get("near_atm_oi_ratio"),
        "opt_flow_ce_pe_oi_diff": rolling.get("ce_pe_oi_diff"),
        "opt_flow_ce_pe_volume_diff": rolling.get("ce_pe_volume_diff"),
        "opt_flow_options_volume_total": rolling.get("options_volume_total"),
        "opt_flow_rel_volume_20": rolling.get("options_rel_volume_20"),
        "time_minute_of_day": rolling.get("time_minute_of_day"),
        "time_day_of_week": rolling.get("time_day_of_week"),
        "time_minute_index": getattr(snap, "minutes_since_open", None),
        "ctx_opening_range_ready": rolling.get("opening_range_ready"),
        "ctx_opening_range_breakout_up": rolling.get("opening_range_breakout_up"),
        "ctx_opening_range_breakout_down": rolling.get("opening_range_breakout_down"),
        "ctx_dte_days": rolling.get("ctx_dte_days"),
        "ctx_is_expiry_day": rolling.get("ctx_is_expiry_day"),
        "ctx_is_near_expiry": rolling.get("ctx_is_near_expiry"),
        "ctx_is_high_vix_day": rolling.get("ctx_is_high_vix_day"),
        "ctx_regime_atr_high": rolling.get("ctx_regime_atr_high"),
        "ctx_regime_atr_low": rolling.get("ctx_regime_atr_low"),
        "ctx_regime_trend_up": rolling.get("ctx_regime_trend_up"),
        "ctx_regime_trend_down": rolling.get("ctx_regime_trend_down"),
        "ctx_regime_expiry_near": rolling.get("ctx_regime_expiry_near"),
    }
    if out["year"] is None and sc.get("date"):
        out["year"] = str(sc.get("date"))[:4]
    for key, value in vel.items():
        out[str(key)] = value
    return out


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _normalise_feature_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        f = float(value)
    except Exception:
        return str(value)
    if not np.isfinite(f):
        return None
    return round(float(f), 10)


def _input_diagnostics(feature_row: dict[str, Any], feature_columns: list[str], *, prob: Optional[float] = None,
                       threshold: Optional[float] = None, recipe_id: Optional[str] = None) -> dict[str, Any]:
    values: list[tuple[str, Any]] = []
    missing: list[str] = []
    non_null = 0
    for col in feature_columns:
        value = _normalise_feature_value(feature_row.get(col))
        if value is None:
            missing.append(col)
        else:
            non_null += 1
        values.append((col, value))
    payload = json.dumps(values, sort_keys=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    out: dict[str, Any] = {
        "feature_count": len(feature_columns),
        "non_null_count": int(non_null),
        "missing_count": int(len(missing)),
        "input_hash": digest[:16],
        "input_hash32": int(digest[:8], 16),
    }
    if missing:
        out["missing_features"] = missing[:20]
    if prob is not None:
        out["predicted_prob"] = float(prob)
        out["output_prob"] = float(prob)
        out["output_col"] = "entry_prob"
    if threshold is not None:
        out["threshold"] = float(threshold)
    if recipe_id is not None:
        out["recipe_id"] = str(recipe_id)
    return out


def _strike_from_bundle(bundle: OptionPnlBundle, snap: Any) -> Optional[int]:
    """Compute the strike per bundle's offset rule using snap.atm_strike.
    Mirrors ml_pipeline_2.labeling.option_pnl._compute_strike."""
    atm = getattr(snap, "atm_strike", None)
    if atm is None or int(atm) <= 0:
        return None
    if bundle.strike_offset_steps == 0:
        return int(atm)
    step_fn = getattr(snap, "strike_step", None)
    step = step_fn() if callable(step_fn) else None
    if step is None or int(step) <= 0:
        return None
    sign = +1 if bundle.option_type == "CE" else -1
    return int(atm) + sign * int(bundle.strike_offset_steps) * int(step)


def build_decision_from_bundle(
    *,
    bundle: OptionPnlBundle,
    snap: Any,
    rolling_features: Optional[dict[str, object]] = None,
) -> StagedRuntimeDecision:
    """Run XGBoost prediction on snapshot features and emit a
    StagedRuntimeDecision compatible with the existing PureMLEngine flow.

    On HOLD (prob below threshold) returns action="HOLD" with reason
    encoding the prob — engine logs it as decision-trace, no trade fires.

    On FIRE returns action="BUY_CE" or "BUY_PE" per bundle.option_type
    with recipe_id, recipe_prob, stop_loss_pct, target_pct, horizon_minutes
    populated from bundle params. risk_basis is "option_premium" so
    trade_signal_builder uses premium-relative stop/target.
    """
    # Build feature row in the exact column order the model expects
    flat = _flatten_snapshot_features(snap, rolling_features)
    base_diag = _input_diagnostics(
        flat,
        bundle.feature_columns,
        threshold=bundle.decision_threshold,
        recipe_id=bundle.recipe_id,
    )
    row = np.array(
        [_safe_float(flat.get(c)) for c in bundle.feature_columns],
        dtype=np.float32,
    ).reshape(1, -1)

    try:
        prob = float(bundle.model.predict_proba(row)[0, 1])
    except Exception as exc:
        return StagedRuntimeDecision(
            action="HOLD",
            reason=f"option_pnl_predict_error:{exc!s}"[:200],
            model_diagnostics={"option_pnl": {**base_diag, "error": str(exc)}, "stage1": base_diag},
        )

    diag = _input_diagnostics(
        flat,
        bundle.feature_columns,
        prob=prob,
        threshold=bundle.decision_threshold,
        recipe_id=bundle.recipe_id,
    )
    diagnostics = {
        "option_pnl": diag,
        # Existing dashboard columns read stage1 hash/non-null/output fields.
        "stage1": dict(diag),
    }

    if prob < bundle.decision_threshold:
        return StagedRuntimeDecision(
            action="HOLD",
            reason=f"prob_below_threshold:{prob:.4f}",
            entry_prob=prob,
            recipe_prob=prob,
            recipe_id=bundle.recipe_id,
            risk_basis="option_premium",
            model_diagnostics=diagnostics,
        )

    action = "BUY_CE" if bundle.option_type == "CE" else "BUY_PE"
    # Pre-select strike using the SAME rule the labeler used (strict ATM or
    # offset_steps*strike_step). Engine sees decision.selected_strike and skips
    # smart-strike entirely. This closes audit-row #3 of OPTION_LABEL_CONTRACT.
    selected_strike = _strike_from_bundle(bundle, snap)
    if selected_strike is None:
        # Same condition the labeler hits: no ATM or no strike_step. We
        # could not pick the strike the labeler would have picked, so we
        # cannot guarantee equivalence — block the trade rather than fall
        # back to smart-strike (which would produce off-label fills).
        return StagedRuntimeDecision(
            action="HOLD",
            reason="missing_atm_or_strike_step_for_bundle",
            entry_prob=prob,
            recipe_prob=prob,
            recipe_id=bundle.recipe_id,
            risk_basis="option_premium",
            model_diagnostics=diagnostics,
        )
    strike_reason = (
        "bundle_atm" if bundle.strike_offset_steps == 0
        else f"bundle_atm_offset_{bundle.strike_offset_steps:+d}"
    )
    return StagedRuntimeDecision(
        action=action,
        reason=f"option_pnl_fire:prob={prob:.4f}",
        entry_prob=prob,
        direction_up_prob=(prob if bundle.option_type == "CE" else 1.0 - prob),
        ce_prob=(1.0 if bundle.option_type == "CE" else 0.0),
        pe_prob=(1.0 if bundle.option_type == "PE" else 0.0),
        recipe_id=bundle.recipe_id,
        recipe_prob=prob,
        recipe_margin=0.0,  # no other recipes competing in a single-recipe bundle
        horizon_minutes=bundle.max_hold_bars,
        stop_loss_pct=bundle.stop_pct_of_premium,
        target_pct=bundle.target_pct_of_premium,
        risk_basis="option_premium",
        model_diagnostics=diagnostics,
        selected_strike=int(selected_strike),
        selected_strike_reason=strike_reason,
    )


def _safe_float(v: Any) -> float:
    """Coerce to float; on failure or NaN, return 0.0 (matches labeler's
    fillna(0.0) at training time)."""
    if v is None:
        return 0.0
    try:
        x = float(v)
        if not np.isfinite(x):
            return 0.0
        return x
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "OptionPnlBundle",
    "load_option_pnl_bundle",
    "load_bundle_from_env",
    "build_decision_from_bundle",
]
