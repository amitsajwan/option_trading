"""Model & data integrity checks for strategy_app.

Runs at startup to detect misconfigured, missing, wrong-kind, or degraded
model bundles BEFORE the first bar is evaluated.  A model silently running
on bad data is worse than a model not running at all.

Failure modes covered:
  FM-1  Entry model not loaded (path unset / file missing)
  FM-2  Wrong bundle kind (entry path points to direction bundle, or vice-versa)
  FM-3  Feature NaN gate not set for entry model (pre-warmup bars fire on garbage)
  FM-4  NaN gate not implemented in direction model code path (code gap warning)
  FM-5  xgboost version mismatch (train vs serve — all-same-prob incident 2026-06-13)
  FM-6  Feature medians missing from bundle (NaN filled with 0.0, not training median)
  FM-7  Entry min-prob misconfigured (wrong threshold → all-suppressed or all-fire)

Severity:
  ERROR   — process should abort; trading will produce zero or corrupt signals
  WARNING — degraded state; system runs but on sub-optimal data

Usage in main.py::

    from .diagnostics.model_integrity import run_integrity_check, format_report
    report = run_integrity_check(dict(os.environ))
    logger.info(format_report(report))
    if not report["ok"]:
        raise ValueError("Model integrity check failed — see MODEL INTEGRITY log lines")
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENTRY_KIND = "entry_only_bundle"
_DIRECTION_KIND = "direction_only_bundle"

_ENTRY_MIN_PROB_WARN_ABOVE = 0.30


def _load_bundle_safe(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Attempt to load a joblib bundle. Returns (bundle, error_msg). Never raises."""
    if not path:
        return None, "path is empty or not configured"
    p = Path(path)
    if not p.exists():
        return None, f"file not found: {path}"
    try:
        import joblib
        bundle = joblib.load(path)
        if not isinstance(bundle, dict):
            return None, f"expected dict bundle, got {type(bundle).__name__}: {path}"
        return bundle, None
    except Exception as exc:
        return None, f"joblib.load failed ({exc}): {path}"


def _check_one_bundle(
    path: str,
    expected_kind: str,
    label: str,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Check a single bundle file. Returns (errors, warnings, info_dict)."""
    errors: List[str] = []
    warnings: List[str] = []
    info: Dict[str, Any] = {"label": label, "path": path}

    bundle, load_err = _load_bundle_safe(path)
    if bundle is None:
        errors.append(f"{label}: {load_err}")
        info["loaded"] = False
        return errors, warnings, info

    info["loaded"] = True

    # FM-2: kind check
    actual_kind = bundle.get("kind")
    if actual_kind != expected_kind:
        errors.append(
            f"{label}: wrong bundle kind — "
            f"expected={expected_kind!r} got={actual_kind!r} "
            f"(did you swap ENTRY_ML_MODEL_PATH and DIRECTION_ML_MODEL_PATH?)"
        )
        info["kind"] = actual_kind
        return errors, warnings, info
    info["kind"] = actual_kind

    # Feature list
    features: List[str] = list(bundle.get("features") or [])
    info["n_features"] = len(features)
    if not features:
        errors.append(f"{label}: bundle has no 'features' list — inference will always return None")

    # FM-6: median coverage
    medians: Dict[str, float] = dict(bundle.get("feature_medians") or {})
    info["n_medians"] = len(medians)
    missing_medians = len(features) - len(medians)
    if missing_medians > 0:
        warnings.append(
            f"{label}: {missing_medians}/{len(features)} features have no training median — "
            f"NaN values will be filled with 0.0 (systematic bias risk)"
        )

    # Holdout AUC — informational only
    holdout_auc = (bundle.get("holdout_eval") or {}).get("roc_auc")
    info["holdout_auc"] = holdout_auc

    # FM-5: xgboost version mismatch (entry bundles only)
    if expected_kind == _ENTRY_KIND:
        try:
            import xgboost as xgb
            live_ver = getattr(xgb, "__version__", "unknown")
            train_ver = (
                bundle.get("xgboost_version")
                or (bundle.get("meta") or {}).get("xgboost_version")
            )
            info["xgb_train_ver"] = train_ver
            info["xgb_serve_ver"] = live_ver
            if train_ver and train_ver != live_ver:
                errors.append(
                    f"{label}: xgboost version mismatch — "
                    f"trained on {train_ver} but serving on {live_ver}. "
                    f"predict_proba output format changed between 2.x and 3.x; "
                    f"all bars may return the same constant probability (incident 2026-06-13). "
                    f"Fix: pin xgboost=={train_ver} in the Dockerfile."
                )
            else:
                info["xgb_version_ok"] = True
        except ImportError:
            warnings.append(
                f"{label}: xgboost not importable — cannot verify train/serve version parity"
            )

    return errors, warnings, info


def run_integrity_check(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run all model integrity checks against the supplied environment dict.

    Pass ``dict(os.environ)`` at startup. Returns::

        {
          "ok": bool,             # False when any ERROR is present
          "errors":   [str, ...], # fatal — abort startup
          "warnings": [str, ...], # degraded — running but on bad data
          "checks": {
              "entry_models": [{"label":..., "loaded":..., ...}, ...],
              "direction_model": {"loaded":..., ...},
              "gates": {"entry_nan_gate":..., "direction_nan_gate":..., ...},
          },
        }
    """
    if env is None:
        env = dict(os.environ)

    all_errors: List[str] = []
    all_warnings: List[str] = []
    checks: Dict[str, Any] = {}

    # ── Entry model(s) ──────────────────────────────────────────────────────
    entry_infos: List[Dict[str, Any]] = []
    primary_path = (env.get("ENTRY_ML_MODEL_PATH") or "").strip()
    if not primary_path:
        all_errors.append(
            "ENTRY_ML_MODEL_PATH is not set — no entry model configured; "
            "ML_ENTRY strategy will produce zero votes (no trades)"
        )
    else:
        errs, warns, info = _check_one_bundle(primary_path, _ENTRY_KIND, "entry_model[m1]")
        all_errors.extend(errs)
        all_warnings.extend(warns)
        entry_infos.append(info)

    # Optional additional entry models ENTRY_ML_MODEL_PATH_2, _3, ...
    for idx in range(2, 10):
        extra_path = (env.get(f"ENTRY_ML_MODEL_PATH_{idx}") or "").strip()
        if not extra_path:
            break
        errs, warns, info = _check_one_bundle(extra_path, _ENTRY_KIND, f"entry_model[m{idx}]")
        all_errors.extend(errs)
        all_warnings.extend(warns)
        entry_infos.append(info)

    checks["entry_models"] = entry_infos

    # ── Direction model ──────────────────────────────────────────────────────
    dir_path = (env.get("DIRECTION_ML_MODEL_PATH") or "").strip()
    if not dir_path:
        all_warnings.append(
            "DIRECTION_ML_MODEL_PATH is not set — direction will use rule-based fallback only "
            "(no ML CE/PE scoring)"
        )
        checks["direction_model"] = {"loaded": False, "reason": "path_not_set"}
    else:
        errs, warns, info = _check_one_bundle(dir_path, _DIRECTION_KIND, "direction_model")
        all_errors.extend(errs)
        all_warnings.extend(warns)
        checks["direction_model"] = info

    # ── Runtime gates ────────────────────────────────────────────────────────
    gates: Dict[str, Any] = {}

    # FM-3: NaN gate for entry model
    nan_gate_raw = (env.get("ENTRY_ML_MAX_NAN_FEATURES") or "").strip()
    if not nan_gate_raw:
        all_warnings.append(
            "ENTRY_ML_MAX_NAN_FEATURES is not set — "
            "velocity features are NaN before 11:30 IST (39+ of 54 features); "
            "median imputation will produce inflated entry probabilities on warmup bars. "
            "Recommended: ENTRY_ML_MAX_NAN_FEATURES=3"
        )
        gates["entry_nan_gate"] = {"set": False}
    else:
        try:
            nan_val = int(nan_gate_raw)
            gates["entry_nan_gate"] = {"set": True, "value": nan_val}
            if nan_val > 10:
                all_warnings.append(
                    f"ENTRY_ML_MAX_NAN_FEATURES={nan_val} is very permissive — "
                    f"warmup bars with 39 NaN features will pass the gate and score with medians. "
                    f"Recommended: 3"
                )
        except ValueError:
            all_warnings.append(
                f"ENTRY_ML_MAX_NAN_FEATURES={nan_gate_raw!r} is not a valid integer — gate is disabled"
            )
            gates["entry_nan_gate"] = {"set": False, "bad_value": nan_gate_raw}

    # FM-4: NaN gate for direction model
    dir_nan_raw = (env.get("DIRECTION_ML_MAX_NAN_FEATURES") or "").strip()
    if not dir_nan_raw:
        all_warnings.append(
            "DIRECTION_ML_MAX_NAN_FEATURES is not set — "
            "direction model scores every bar even when many features are NaN (medians fill). "
            "Current known NaN count: 3/75 (vix_current, vix_intraday_chg, iv_pct_rank_session). "
            "Recommended: DIRECTION_ML_MAX_NAN_FEATURES=10"
        )
        gates["direction_nan_gate"] = {"set": False}
    else:
        try:
            gates["direction_nan_gate"] = {"set": True, "value": int(dir_nan_raw)}
        except ValueError:
            all_warnings.append(
                f"DIRECTION_ML_MAX_NAN_FEATURES={dir_nan_raw!r} is not a valid integer — gate is disabled"
            )
            gates["direction_nan_gate"] = {"set": False, "bad_value": dir_nan_raw}

    # FM-7: entry min-prob sanity
    min_prob_raw = (env.get("ENTRY_ML_MIN_PROB") or "").strip()
    if not min_prob_raw:
        all_warnings.append(
            "ENTRY_ML_MIN_PROB is not set — using hardcoded default 0.55 "
            "(velocity_base expects ~0.049; entry_only_v3 expects ~0.45)"
        )
        gates["entry_min_prob"] = {"set": False}
    else:
        try:
            mp = float(min_prob_raw)
            gates["entry_min_prob"] = {"set": True, "value": mp}
            if mp > _ENTRY_MIN_PROB_WARN_ABOVE:
                all_warnings.append(
                    f"ENTRY_ML_MIN_PROB={mp} looks high for the configured model. "
                    f"velocity_base typical range: 0.03–0.10. "
                    f"entry_only_v3 typical range: 0.40–0.50. "
                    f"A wrong threshold suppresses nearly all entries or fires on every bar."
                )
        except ValueError:
            all_warnings.append(
                f"ENTRY_ML_MIN_PROB={min_prob_raw!r} is not a valid float"
            )
            gates["entry_min_prob"] = {"set": False, "bad_value": min_prob_raw}

    checks["gates"] = gates

    ok = len(all_errors) == 0
    return {
        "ok": ok,
        "errors": all_errors,
        "warnings": all_warnings,
        "checks": checks,
    }


def format_report(report: Dict[str, Any]) -> str:
    """Human-readable integrity board. Designed for `docker logs | grep 'MODEL INTEGRITY'`."""
    lines: List[str] = []
    ok = report.get("ok", False)
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    checks = report.get("checks") or {}

    lines.append("MODEL INTEGRITY ┌────────────────────────────────────────────────────")
    lines.append(f"MODEL INTEGRITY │ status: {'✓ OK' if ok else '✗ ERRORS DETECTED — see [!] lines below'}")

    for entry in checks.get("entry_models") or []:
        auc = entry.get("holdout_auc")
        xgb = ""
        if entry.get("xgb_train_ver"):
            match = entry.get("xgb_version_ok", False)
            xgb = f"  xgb={'✓' if match else '✗ MISMATCH'}(train={entry['xgb_train_ver']}/serve={entry.get('xgb_serve_ver','?')})"
        lines.append(
            f"MODEL INTEGRITY │  {entry.get('label','?'):<22} "
            f"loaded={entry.get('loaded')}  kind={entry.get('kind','?')}  "
            f"features={entry.get('n_features','?')}  medians={entry.get('n_medians','?')}  "
            f"holdout_auc={auc or '?'}{xgb}"
        )

    dir_info = checks.get("direction_model") or {}
    if dir_info.get("loaded"):
        auc = dir_info.get("holdout_auc")
        lines.append(
            f"MODEL INTEGRITY │  {'direction_model':<22} "
            f"loaded={dir_info.get('loaded')}  kind={dir_info.get('kind','?')}  "
            f"features={dir_info.get('n_features','?')}  medians={dir_info.get('n_medians','?')}  "
            f"holdout_auc={auc or '?'}"
        )
    else:
        lines.append(
            f"MODEL INTEGRITY │  {'direction_model':<22} loaded=False  reason={dir_info.get('reason','?')}"
        )

    gates = checks.get("gates") or {}
    eng = gates.get("entry_nan_gate") or {}
    dng = gates.get("direction_nan_gate") or {}
    emp = gates.get("entry_min_prob") or {}
    lines.append(
        f"MODEL INTEGRITY │  gates: "
        f"entry_nan={str(eng.get('value')) if eng.get('set') else '⚠ NOT SET'}  "
        f"dir_nan={str(dng.get('value')) if dng.get('set') else '⚠ NOT SET'}  "
        f"entry_min_prob={str(emp.get('value')) if emp.get('set') else '⚠ NOT SET'}"
    )

    for msg in errors:
        lines.append(f"MODEL INTEGRITY │  [!] ERROR:   {msg}")
    for msg in warnings:
        lines.append(f"MODEL INTEGRITY │  [~] WARNING: {msg}")

    lines.append("MODEL INTEGRITY └────────────────────────────────────────────────────")
    return "\n".join(lines)


__all__ = ["run_integrity_check", "format_report"]
