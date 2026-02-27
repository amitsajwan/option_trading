import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import joblib


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _merge_bundle(existing: Dict[str, object], incoming: Dict[str, object], trained_side: str) -> Dict[str, object]:
    side = str(trained_side).lower()
    if side not in ("ce", "pe"):
        return dict(incoming)
    merged = dict(existing)

    # Validate feature column consistency before merging.
    # CE and PE must share the same feature set (produced by the same pipeline run).
    existing_cols = merged.get("feature_columns", [])
    incoming_cols = incoming.get("feature_columns", [])
    if existing_cols and incoming_cols and list(existing_cols) != list(incoming_cols):
        import warnings
        warnings.warn(
            f"registry merge: feature_columns mismatch between existing bundle and incoming "
            f"{side.upper()} model. Existing has {len(existing_cols)} cols, incoming has "
            f"{len(incoming_cols)} cols. Overwriting with incoming. Ensure both sides were "
            "trained from the same feature pipeline run.",
            RuntimeWarning,
        )

    merged.update(
        {
            "model_type": incoming.get("model_type", merged.get("model_type", "lightgbm_dual")),
            "feature_columns": incoming.get("feature_columns", merged.get("feature_columns", [])),
            "feature_profile": incoming.get("feature_profile", merged.get("feature_profile")),
            "calibration_method": incoming.get("calibration_method", merged.get("calibration_method", "none")),
            "trained_side": "both" if ("ce_model" in merged and "pe_model" in merged) else side,
        }
    )
    model_key = f"{side}_model"
    thr_key = f"{side}_threshold"
    if model_key in incoming:
        merged[model_key] = incoming[model_key]
    if thr_key in incoming:
        merged[thr_key] = incoming[thr_key]
    merged["trained_side"] = "both" if ("ce_model" in merged and "pe_model" in merged) else side
    return merged


def publish_model_bundle(
    *,
    root: Path,
    model_group: str,
    profile_id: str,
    bundle: Dict[str, object],
    report: Dict[str, object],
) -> Dict[str, str]:
    group_root = root / str(model_group)
    model_dir = group_root / "model"
    profile_dir = group_root / "config" / "profiles" / str(profile_id)
    reports_dir = group_root / "reports" / "training"
    model_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "model.joblib"
    report_path = reports_dir / f"{profile_id}_modeling_report.json"
    threshold_path = profile_dir / "threshold_report.json"
    contract_path = group_root / "model_contract.json"

    trained_side = str(report.get("trained_side", "both")).lower()
    final_bundle = dict(bundle)
    if model_path.exists():
        try:
            existing_bundle = joblib.load(model_path)
            if isinstance(existing_bundle, dict):
                final_bundle = _merge_bundle(existing_bundle, bundle, trained_side)
        except Exception:
            final_bundle = dict(bundle)
    joblib.dump(final_bundle, model_path)
    _write_json(report_path, report)
    threshold_report = {
        "created_at_utc": _utc_now(),
        "trained_side": final_bundle.get("trained_side", report.get("trained_side", "both")),
        "ce_threshold": final_bundle.get("ce_threshold"),
        "pe_threshold": final_bundle.get("pe_threshold"),
        "source_report": str(report_path).replace("\\", "/"),
    }
    _write_json(threshold_path, threshold_report)
    contract = {
        "schema_version": "1.0",
        "model_group": str(model_group),
        "required_features": [str(c) for c in final_bundle.get("feature_columns", [])],
        "allow_extra_features": True,
        "missing_policy": "error",
    }
    _write_json(contract_path, contract)
    return {
        "model_joblib": str(model_path).replace("\\", "/"),
        "modeling_report_json": str(report_path).replace("\\", "/"),
        "threshold_report_json": str(threshold_path).replace("\\", "/"),
        "model_contract_json": str(contract_path).replace("\\", "/"),
    }
