import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import joblib
import pandas as pd

from .diagnostics_stress import _load_thresholds, _summary_md, run_diagnostics_stress
from .order_intent_runtime import _load_jsonl, _summary_markdown, build_fills, build_order_intents, run_order_intent_runtime
from .reproducibility_runner import compare_artifact_sets


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_phase3_once(
    *,
    labeled_data_path: Path,
    model_package_path: Path,
    threshold_report_path: Path,
    decisions_jsonl_path: Path,
    run_dir: Path,
) -> Dict[str, object]:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    labeled_df = pd.read_parquet(labeled_data_path)
    model_package = joblib.load(model_package_path)
    ce_thr, pe_thr = _load_thresholds(threshold_report_path)

    t32 = run_diagnostics_stress(
        labeled_df=labeled_df,
        model_package=model_package,
        ce_threshold=float(ce_thr),
        pe_threshold=float(pe_thr),
    )
    t32_json = artifacts / "t32_diagnostics_stress_report.json"
    t32_md = artifacts / "t32_diagnostics_stress_summary.md"
    _write_json(t32_json, t32)
    _write_md(t32_md, _summary_md(t32))

    decisions = _load_jsonl(decisions_jsonl_path)
    t33 = run_order_intent_runtime(decision_events=decisions)
    t33_json = artifacts / "t33_order_runtime_report.json"
    t33_md = artifacts / "t33_order_runtime_summary.md"
    _write_json(t33_json, t33)
    _write_md(t33_md, _summary_markdown(t33))

    intents = build_order_intents(decisions)["intents"]
    fills = build_fills(decisions)["fills"]
    intents_out = artifacts / "t33_order_intents.parquet"
    fills_out = artifacts / "t33_order_fills.parquet"
    intents.to_parquet(intents_out, index=False)
    fills.to_parquet(fills_out, index=False)

    return {
        "run_dir": str(run_dir),
        "artifacts_dir": str(artifacts),
        "rows_labeled": int(len(labeled_df)),
        "decision_events": int(len(decisions)),
    }


def _summary_markdown_phase3(report: Dict[str, object]) -> str:
    cmp = report["comparison"]
    lines = [
        "# Phase3 Reproducibility Summary (T34)",
        "",
        f"- Status: `{report['status']}`",
        f"- Generated: `{report['created_at_utc']}`",
        f"- Labeled data: `{report['inputs']['labeled_data_path']}`",
        f"- Model package: `{report['inputs']['model_package_path']}`",
        f"- Threshold report: `{report['inputs']['threshold_report_path']}`",
        f"- Decisions: `{report['inputs']['decisions_jsonl_path']}`",
        f"- Run1 dir: `{report['run1_dir']}`",
        f"- Run2 dir: `{report['run2_dir']}`",
        f"- Artifacts compared: `{cmp['artifacts_compared']}` / `{cmp['artifacts_checked']}`",
        f"- Mismatches: `{cmp['mismatch_count']}`",
        "",
        "## Comparison",
        "",
    ]
    if cmp["mismatch_count"] == 0:
        lines.append("- pass")
    else:
        for mm in cmp["mismatches"]:
            lines.append(f"- {mm['artifact']}: {mm['reason']}")
    return "\n".join(lines) + "\n"


def run_phase3_reproducibility(
    *,
    labeled_data_path: Path,
    model_package_path: Path,
    threshold_report_path: Path,
    decisions_jsonl_path: Path,
    workdir: Path,
) -> Dict[str, object]:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    run1 = workdir / "run1"
    run2 = workdir / "run2"

    run1_meta = run_phase3_once(
        labeled_data_path=labeled_data_path,
        model_package_path=model_package_path,
        threshold_report_path=threshold_report_path,
        decisions_jsonl_path=decisions_jsonl_path,
        run_dir=run1,
    )
    run2_meta = run_phase3_once(
        labeled_data_path=labeled_data_path,
        model_package_path=model_package_path,
        threshold_report_path=threshold_report_path,
        decisions_jsonl_path=decisions_jsonl_path,
        run_dir=run2,
    )

    artifacts = [
        "artifacts/t32_diagnostics_stress_report.json",
        "artifacts/t33_order_runtime_report.json",
        "artifacts/t33_order_intents.parquet",
        "artifacts/t33_order_fills.parquet",
    ]
    comparison = compare_artifact_sets(run1_dir=run1, run2_dir=run2, artifacts=artifacts)
    status = "pass" if comparison.get("mismatch_count", 1) == 0 else "fail"
    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "inputs": {
            "labeled_data_path": str(labeled_data_path),
            "model_package_path": str(model_package_path),
            "threshold_report_path": str(threshold_report_path),
            "decisions_jsonl_path": str(decisions_jsonl_path),
        },
        "workdir": str(workdir),
        "run1_dir": str(run1),
        "run2_dir": str(run2),
        "run1": run1_meta,
        "run2": run2_meta,
        "comparison": comparison,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 reproducibility runner (T34)")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t29_2y_auto_t05_labeled_features.parquet")
    parser.add_argument("--model-package", default="ml_pipeline/artifacts/t29_2y_auto_best_model.joblib")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t31_calibration_threshold_report.json")
    parser.add_argument("--decisions-jsonl", default="ml_pipeline/artifacts/t33_paper_capital_events_actual.jsonl")
    parser.add_argument("--workdir", default="ml_pipeline/artifacts/t34_phase3_reproducibility")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t34_phase3_reproducibility_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t34_phase3_reproducibility_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled = Path(args.labeled_data)
    model = Path(args.model_package)
    threshold = Path(args.threshold_report)
    decisions = Path(args.decisions_jsonl)
    for p, name in ((labeled, "labeled-data"), (model, "model-package"), (threshold, "threshold-report"), (decisions, "decisions-jsonl")):
        if not p.exists():
            print(f"ERROR: {name} not found: {p}")
            return 2

    report = run_phase3_reproducibility(
        labeled_data_path=labeled,
        model_package_path=model,
        threshold_report_path=threshold,
        decisions_jsonl_path=decisions,
        workdir=Path(args.workdir),
    )

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_out.write_text(_summary_markdown_phase3(report), encoding="utf-8")

    print(f"Status: {report['status']}")
    print(f"Mismatches: {report['comparison']['mismatch_count']}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
