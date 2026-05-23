"""Export a direction_dual_bundle.joblib from completed CE and PE research runs.

Reads best stage2 models from two separate research runs (one trained with ce_win_v1,
one with pe_win_v1) and packages them as a direction_dual_bundle compatible with
strategy_app DIRECTION_ML_MODEL_PATH.

At runtime _resolve_direction_dual() picks whichever side has higher P(win) above 0.5.

Usage:
    python -m ml_pipeline_2.scripts.export_direction_dual_bundle \\
        --ce-run-dir ml_pipeline_2/artifacts/research/direction_dual_ce_hpo_v1_20260523_120000 \\
        --pe-run-dir ml_pipeline_2/artifacts/research/direction_dual_pe_hpo_v1_20260523_130000 \\
        --output-dir ml_pipeline_2/artifacts/direction_dual/published
"""
from __future__ import annotations

import argparse
import json
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
    for key in ("direction", "move"):
        if key in models:
            return models[key], features, key
    if len(models) == 1:
        key = next(iter(models.keys()))
        return models[key], features, str(key)
    raise ValueError(f"cannot resolve sklearn model from package keys: {sorted(models.keys())}")


def _load_sub_bundle(run_dir: Path, side: str) -> Dict[str, Any]:
    stage2_model = run_dir / "stages" / "stage2" / "model.joblib"
    if not stage2_model.is_file():
        raise FileNotFoundError(f"missing stage2 model for {side}: {stage2_model}")

    package = joblib.load(stage2_model)
    if not isinstance(package, dict):
        raise ValueError(f"expected dict model package for {side} at {stage2_model}")
    if package.get("_bypass_stage2"):
        raise ValueError(f"stage2 was bypassed for {side} — nothing to export")

    model, features, model_key = _extract_sklearn_model(package)
    if not features:
        contract_path = run_dir / "stages" / "stage2" / "feature_contract.json"
        if contract_path.is_file():
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            features = [str(c) for c in list(contract.get("required_features") or [])]

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

    return {
        "kind": "direction_only_bundle",
        "side": side,
        "model": model,
        "features": features,
        "feature_medians": {f: 0.0 for f in features},
        "label_map": {"CE": 1, "PE": 0},
        "research_run_id": run_dir.name,
        "research_run_dir": str(run_dir),
        "model_key": model_key,
        "feature_set": str((package.get("selected_feature_set") or "")),
        "selected_model": dict(package.get("selected_model") or {}),
        "holdout_eval": {"roc_auc": holdout_auc},
        "stage2_policy": stage2_policy,
        "publishable": publishable,
    }


def export_dual_bundle(
    ce_run_dir: Path,
    pe_run_dir: Path,
    output_dir: Path,
) -> Path:
    ce_run_dir = ce_run_dir.resolve()
    pe_run_dir = pe_run_dir.resolve()

    ce_sub = _load_sub_bundle(ce_run_dir, "CE")
    pe_sub = _load_sub_bundle(pe_run_dir, "PE")

    trained_at = datetime.now(timezone.utc).isoformat()
    bundle: Dict[str, Any] = {
        "kind": "direction_dual_bundle",
        "source": "direction_dual_research_export_v1",
        "ce_bundle": ce_sub,
        "pe_bundle": pe_sub,
        "ce_run_dir": str(ce_run_dir),
        "pe_run_dir": str(pe_run_dir),
        "ce_holdout_auc": (ce_sub.get("holdout_eval") or {}).get("roc_auc"),
        "pe_holdout_auc": (pe_sub.get("holdout_eval") or {}).get("roc_auc"),
        "trained_at": trained_at,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "direction_dual_model.joblib"
    joblib.dump(bundle, out_path)

    report = {
        "exported_at": trained_at,
        "ce_run_dir": str(ce_run_dir),
        "pe_run_dir": str(pe_run_dir),
        "model_path": str(out_path),
        "ce_n_features": len(ce_sub.get("features") or []),
        "pe_n_features": len(pe_sub.get("features") or []),
        "ce_holdout_roc_auc": bundle["ce_holdout_auc"],
        "pe_holdout_roc_auc": bundle["pe_holdout_auc"],
        "ce_publishable": ce_sub.get("publishable"),
        "pe_publishable": pe_sub.get("publishable"),
    }
    (output_dir / "direction_dual_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"CE model: {len(ce_sub.get('features') or [])} features, holdout AUC={bundle['ce_holdout_auc']}")
    print(f"PE model: {len(pe_sub.get('features') or [])} features, holdout AUC={bundle['pe_holdout_auc']}")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ce-run-dir", required=True, help="Completed CE research run directory")
    parser.add_argument("--pe-run-dir", required=True, help="Completed PE research run directory")
    parser.add_argument(
        "--output-dir",
        default=str(_REPO / "ml_pipeline_2" / "artifacts" / "direction_dual" / "published"),
    )
    args = parser.parse_args(argv)

    out = export_dual_bundle(Path(args.ce_run_dir), Path(args.pe_run_dir), Path(args.output_dir))
    print(f"Exported → {out}")
    print("strategy_app:")
    print(f"  export DIRECTION_ML_MODEL_PATH={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
