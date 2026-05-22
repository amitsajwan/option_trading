"""Export entry_only_bundle.joblib from a completed S1 research run.

Usage:
    python -m ml_pipeline_2.scripts.export_entry_bundle_from_research \\
        --run-dir ml_pipeline_2/artifacts/research/direction_only_hpo_v1_20260522_063542 \\
        --output-dir ml_pipeline_2/artifacts/entry_only/published
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib

_REPO = Path(__file__).resolve().parents[2]


def _extract_sklearn_model(package: Dict[str, Any]) -> tuple[Any, List[str], str]:
    features = [str(c) for c in list(package.get("feature_columns") or [])]
    models = dict(package.get("models") or {})
    single_target = package.get("single_target") if isinstance(package.get("single_target"), dict) else {}
    if isinstance(single_target, dict):
        model_key = str(single_target.get("model_key") or "").strip()
        if model_key and model_key in models:
            return models[model_key], features, model_key
    for key in ("entry", "move"):
        if key in models:
            return models[key], features, str(key)
    if len(models) == 1:
        key = next(iter(models.keys()))
        return models[key], features, str(key)
    raise ValueError(f"cannot resolve sklearn model from package keys: {sorted(models.keys())}")


def export_bundle(run_dir: Path, output_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    stage1_model = run_dir / "stages" / "stage1" / "model.joblib"
    if not stage1_model.is_file():
        raise FileNotFoundError(f"missing stage1 model: {stage1_model}")

    package = joblib.load(stage1_model)
    if not isinstance(package, dict):
        raise ValueError(f"expected dict model package at {stage1_model}")
    if package.get("_bypass_stage1"):
        raise ValueError("stage1 was bypassed — nothing to export")

    model, features, model_key = _extract_sklearn_model(package)
    if not features:
        contract_path = run_dir / "stages" / "stage1" / "feature_contract.json"
        if contract_path.is_file():
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            features = [str(c) for c in list(contract.get("required_features") or [])]

    summary_path = run_dir / "summary.json"
    holdout_auc: Optional[float] = None
    stage1_policy: Dict[str, Any] = {}
    publishable = False
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        holdout = (summary.get("holdout_reports") or {}).get("stage1") or {}
        holdout_auc = holdout.get("roc_auc")
        stage1_policy = (summary.get("policy_reports") or {}).get("stage1") or {}
        publishable = bool((summary.get("publish_assessment") or {}).get("publishable"))

    bundle: Dict[str, Any] = {
        "kind": "entry_only_bundle",
        "source": "entry_s1_research_export_v1",
        "model": model,
        "features": features,
        "feature_medians": {f: 0.0 for f in features},
        "research_run_id": run_dir.name,
        "research_run_dir": str(run_dir),
        "model_key": model_key,
        "feature_set": str((package.get("selected_feature_set") or "")),
        "selected_model": dict(package.get("selected_model") or {}),
        "holdout_eval": {"roc_auc": holdout_auc},
        "stage1_policy": stage1_policy,
        "publishable": publishable,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "entry_only_model.joblib"
    joblib.dump(bundle, out_path)

    report = {
        "exported_at": bundle["trained_at"],
        "run_dir": str(run_dir),
        "model_path": str(out_path),
        "n_features": len(features),
        "holdout_roc_auc": holdout_auc,
        "publishable": publishable,
        "stage1_policy": stage1_policy,
    }
    (output_dir / "entry_only_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--output-dir",
        default=str(_REPO / "ml_pipeline_2" / "artifacts" / "entry_only" / "published"),
    )
    args = parser.parse_args(argv)
    out = export_bundle(Path(args.run_dir), Path(args.output_dir))
    print(f"Exported → {out}")
    print(f"  export ENTRY_ML_MODEL_PATH={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
