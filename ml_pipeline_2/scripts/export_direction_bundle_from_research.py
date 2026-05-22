"""Export a direction_only_bundle.joblib from a completed S2-only research run.

Reads stages/stage2/model.joblib (ml_pipeline_2 research package) and writes a bundle
compatible with strategy_app DIRECTION_ML_MODEL_PATH / DirectionMLConflictResolver.

Usage:
    python -m ml_pipeline_2.scripts.export_direction_bundle_from_research \\
        --run-dir ml_pipeline_2/artifacts/research/direction_s2_only_hpo_v1_20260522_120000 \\
        --output-dir ml_pipeline_2/artifacts/direction_only/published

Optional policy thresholds from summary.json stage2 policy are stored in the bundle
for operator reference (runtime still uses ML prob + env thresholds).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]


def _extract_sklearn_model(package: Dict[str, Any]) -> tuple[Any, List[str], str]:
    features = [str(c) for c in list(package.get("feature_columns") or [])]
    models = dict(package.get("models") or {})
    single_target = package.get("single_target") if isinstance(package.get("single_target"), dict) else {}
    if isinstance(single_target, dict):
        model_key = str(single_target.get("model_key") or "").strip()
        if model_key and model_key in models:
            return models[model_key], features, model_key
    for key in ("direction", "move"):
        if key in models:
            return models[key], features, key
    if len(models) == 1:
        key = next(iter(models.keys()))
        return models[key], features, str(key)
    raise ValueError(f"cannot resolve sklearn model from package keys: {sorted(models.keys())}")


def _feature_medians_from_training_report(path: Path, features: List[str]) -> Dict[str, float]:
    if not path.is_file():
        return {f: 0.0 for f in features}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {f: 0.0 for f in features}
    # Training report may not include medians; default 0.0 (matches train_direction_only fallback).
    return {f: 0.0 for f in features}


def export_bundle(
    run_dir: Path,
    output_dir: Path,
    *,
    midday_only: bool = True,
) -> Path:
    run_dir = run_dir.resolve()
    stage2_model = run_dir / "stages" / "stage2" / "model.joblib"
    if not stage2_model.is_file():
        raise FileNotFoundError(f"missing stage2 model: {stage2_model}")

    package = joblib.load(stage2_model)
    if not isinstance(package, dict):
        raise ValueError(f"expected dict model package at {stage2_model}")
    if package.get("_bypass_stage2"):
        raise ValueError("stage2 was bypassed — nothing to export")

    model, features, model_key = _extract_sklearn_model(package)
    if not features:
        contract_path = run_dir / "stages" / "stage2" / "feature_contract.json"
        if contract_path.is_file():
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            features = [str(c) for c in list(contract.get("required_features") or [])]

    medians = _feature_medians_from_training_report(
        run_dir / "stages" / "stage2" / "training_report.json",
        features,
    )

    summary_path = run_dir / "summary.json"
    holdout_auc: Optional[float] = None
    stage2_policy: Dict[str, Any] = {}
    publishable = False
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        holdout = (summary.get("holdout_reports") or {}).get("stage2") or {}
        holdout_auc = holdout.get("roc_auc")
        stage2_policy = (summary.get("policy_reports") or {}).get("stage2") or {}
        publishable = bool((summary.get("publish_assessment") or {}).get("publishable"))

    bundle: Dict[str, Any] = {
        "kind": "direction_only_bundle",
        "source": "direction_s2_research_export_v1",
        "model": model,
        "features": features,
        "feature_medians": medians,
        "label_map": {"CE": 1, "PE": 0},
        "research_run_id": run_dir.name,
        "research_run_dir": str(run_dir),
        "model_key": model_key,
        "feature_set": str((package.get("selected_feature_set") or "")),
        "selected_model": dict(package.get("selected_model") or {}),
        "holdout_eval": {"roc_auc": holdout_auc},
        "stage2_policy": stage2_policy,
        "publishable": publishable,
        "midday_only": bool(midday_only),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "direction_only_model.joblib"
    joblib.dump(bundle, out_path)

    report = {
        "exported_at": bundle["trained_at"],
        "run_dir": str(run_dir),
        "model_path": str(out_path),
        "n_features": len(features),
        "holdout_roc_auc": holdout_auc,
        "publishable": publishable,
        "stage2_policy": stage2_policy,
    }
    (output_dir / "direction_only_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True, help="Completed research run directory")
    parser.add_argument(
        "--output-dir",
        default=str(_REPO / "ml_pipeline_2" / "artifacts" / "direction_only" / "published"),
    )
    args = parser.parse_args(argv)

    out = export_bundle(Path(args.run_dir), Path(args.output_dir))
    print(f"Exported → {out}")
    print("strategy_app:")
    print(f"  export DIRECTION_ML_MODEL_PATH={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
