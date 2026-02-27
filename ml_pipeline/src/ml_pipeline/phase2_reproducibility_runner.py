import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .reproducibility_runner import compare_artifact_sets
from .schema_validator import resolve_archive_base

PHASE1_REQUIRED_ARTIFACTS = (
    "t04_features.parquet",
    "t06_baseline_model.joblib",
    "t08_threshold_report.json",
    "t11_paper_decisions.jsonl",
)


def _run_cmd(cmd: List[str], env: Dict[str, str]) -> None:
    completed = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "Command failed\n"
            + f"cmd: {' '.join(cmd)}\n"
            + f"stdout:\n{completed.stdout}\n"
            + f"stderr:\n{completed.stderr}"
        )


def _ensure_required_artifacts(base_dir: Path, required_files: Sequence[str]) -> None:
    missing = [name for name in required_files if not (base_dir / name).exists()]
    if missing:
        raise RuntimeError(
            "Missing required artifacts for phase2 reproducibility\n"
            + f"base_dir: {base_dir}\n"
            + f"missing: {', '.join(missing)}"
        )


def _bootstrap_phase1_artifacts(base_path: Path, root: Path, phase1_workdir: Path) -> Path:
    py = sys.executable
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "ml_pipeline" / "src")
    env["LOCAL_HISTORICAL_BASE"] = str(base_path)
    report_out = phase1_workdir / "t13_reproducibility_report.json"
    summary_out = phase1_workdir / "t13_reproducibility_summary.md"
    cmd = [
        py,
        "-m",
        "ml_pipeline.reproducibility_runner",
        "--base-path",
        str(base_path),
        "--workdir",
        str(phase1_workdir),
        "--single-run",
        "--report-out",
        str(report_out),
        "--summary-out",
        str(summary_out),
    ]
    _run_cmd(cmd, env=env)
    artifacts_dir = phase1_workdir / "run1" / "artifacts"
    _ensure_required_artifacts(artifacts_dir, PHASE1_REQUIRED_ARTIFACTS)
    return artifacts_dir


def _resolve_phase1_artifacts_dir(
    *,
    base_path: Path,
    root: Path,
    workdir: Path,
    phase1_artifacts_dir: Optional[str],
    bootstrap_phase1: bool,
) -> Tuple[Path, Dict[str, object]]:
    if phase1_artifacts_dir:
        resolved = Path(phase1_artifacts_dir)
        _ensure_required_artifacts(resolved, PHASE1_REQUIRED_ARTIFACTS)
        return resolved, {"mode": "explicit", "phase1_artifacts_dir": str(resolved)}

    if bootstrap_phase1:
        bootstrap_dir = workdir / "phase1_seed"
        artifacts = _bootstrap_phase1_artifacts(base_path=base_path, root=root, phase1_workdir=bootstrap_dir)
        return artifacts, {"mode": "bootstrapped_t13_single_run", "phase1_artifacts_dir": str(artifacts)}

    fallback = root / "ml_pipeline" / "artifacts"
    _ensure_required_artifacts(fallback, PHASE1_REQUIRED_ARTIFACTS)
    return fallback, {"mode": "fallback_root_artifacts", "phase1_artifacts_dir": str(fallback)}


def _phase2_commands(
    base_path: Path,
    run_artifacts: Path,
    phase1_artifacts: Path,
) -> List[List[str]]:
    py = sys.executable
    t04 = phase1_artifacts / "t04_features.parquet"
    t06 = phase1_artifacts / "t06_baseline_model.joblib"
    t08 = phase1_artifacts / "t08_threshold_report.json"
    t11 = phase1_artifacts / "t11_paper_decisions.jsonl"
    return [
        [
            py,
            "-m",
            "ml_pipeline.exit_policy",
            "--report-out",
            str(run_artifacts / "t14_exit_policy_validation_report.json"),
            "--normalized-out",
            str(run_artifacts / "t14_exit_policy_config.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.label_engine",
            "--features",
            str(t04),
            "--base-path",
            str(base_path),
            "--out",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--report-out",
            str(run_artifacts / "t05_label_report.json"),
            "--path-report-out",
            str(run_artifacts / "t15_label_path_report.json"),
            "--stop-loss-pct",
            "0.12",
            "--take-profit-pct",
            "0.24",
        ],
        [
            py,
            "-m",
            "ml_pipeline.backtest_engine",
            "--labeled-data",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--threshold-report",
            str(t08),
            "--execution-mode",
            "path_v2",
            "--intrabar-tie-break",
            "sl",
            "--slippage-per-trade",
            "0.0002",
            "--trades-out",
            str(run_artifacts / "t16_backtest_trades.parquet"),
            "--report-out",
            str(run_artifacts / "t16_backtest_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.dynamic_exit_policy",
            "--out",
            str(run_artifacts / "t17_dynamic_exit_policy_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.exit_policy_optimization",
            "--labeled-data",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--threshold-report",
            str(t08),
            "--tie-break-grid",
            "sl,tp",
            "--slippage-grid",
            "0.0,0.0002,0.0005",
            "--forced-eod-grid",
            "15:24",
            "--report-out",
            str(run_artifacts / "t18_exit_policy_optimization_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.strategy_comparison_v2",
            "--labeled-data",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--threshold-report",
            str(t08),
            "--t18-report",
            str(run_artifacts / "t18_exit_policy_optimization_report.json"),
            "--report-out",
            str(run_artifacts / "t19_strategy_comparison_v2_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.backtest_engine",
            "--labeled-data",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--threshold-report",
            str(t08),
            "--execution-mode",
            "path_v2",
            "--fill-model",
            "liquidity_adjusted",
            "--fill-spread-fraction",
            "0.5",
            "--fill-volume-impact",
            "0.02",
            "--fill-min",
            "0.0",
            "--fill-max",
            "0.01",
            "--slippage-per-trade",
            "0.0002",
            "--trades-out",
            str(run_artifacts / "t20_backtest_trades.parquet"),
            "--report-out",
            str(run_artifacts / "t20_backtest_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.paper_replay_evaluation",
            "--decisions-jsonl",
            str(t11),
            "--labeled-data",
            str(run_artifacts / "t05_labeled_features.parquet"),
            "--threshold-report",
            str(t08),
            "--t19-report",
            str(run_artifacts / "t19_strategy_comparison_v2_report.json"),
            "--trades-out",
            str(run_artifacts / "t21_replay_evaluation_trades.parquet"),
            "--report-out",
            str(run_artifacts / "t21_replay_evaluation_report.json"),
        ],
        [
            py,
            "-m",
            "ml_pipeline.live_inference_adapter",
            "--run-mode",
            "replay-dry-run-v2",
            "--mode",
            "dual",
            "--model-package",
            str(t06),
            "--threshold-report",
            str(t08),
            "--feature-parquet",
            str(t04),
            "--output-jsonl",
            str(run_artifacts / "t22_exit_aware_paper_events.jsonl"),
            "--limit",
            "300",
            "--max-hold-minutes",
            "5",
            "--confidence-buffer",
            "0.05",
        ],
        [
            py,
            "-m",
            "ml_pipeline.monitoring_execution",
            "--reference-events",
            str(run_artifacts / "t22_exit_aware_paper_events.jsonl"),
            "--current-events",
            str(run_artifacts / "t22_exit_aware_paper_events.jsonl"),
            "--report-out",
            str(run_artifacts / "t23_execution_monitoring_report.json"),
            "--summary-out",
            str(run_artifacts / "t23_execution_monitoring_summary.md"),
        ],
    ]


def _run_phase2_once(base_path: Path, root: Path, run_dir: Path, phase1_artifacts: Path) -> Dict[str, object]:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "ml_pipeline" / "src")
    env["LOCAL_HISTORICAL_BASE"] = str(base_path)

    cmds = _phase2_commands(base_path=base_path, run_artifacts=artifacts, phase1_artifacts=phase1_artifacts)
    for cmd in cmds:
        _run_cmd(cmd, env=env)

    return {"run_dir": str(run_dir), "artifacts_dir": str(artifacts), "commands_executed": int(len(cmds))}


def _summary_markdown(report: Dict[str, object]) -> str:
    cmp = report["comparison"]
    lines = []
    lines.append("# Phase2 Reproducibility Summary (T24)")
    lines.append("")
    lines.append(f"- Status: `{report['status']}`")
    lines.append(f"- Generated: `{report['created_at_utc']}`")
    lines.append(f"- Base path: `{report['base_path']}`")
    lines.append(f"- Phase1 artifacts mode: `{report['phase1_artifacts']['mode']}`")
    lines.append(f"- Phase1 artifacts dir: `{report['phase1_artifacts']['phase1_artifacts_dir']}`")
    lines.append(f"- Run1 dir: `{report['run1_dir']}`")
    lines.append(f"- Run2 dir: `{report['run2_dir']}`")
    lines.append(f"- Artifacts compared: `{cmp['artifacts_compared']}` / `{cmp['artifacts_checked']}`")
    lines.append(f"- Mismatches: `{cmp['mismatch_count']}`")
    lines.append("")
    lines.append("## Comparison")
    lines.append("")
    if cmp["mismatch_count"] == 0:
        lines.append("- pass")
    else:
        for mm in cmp["mismatches"]:
            lines.append(f"- {mm['artifact']}: {mm['reason']}")
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2 reproducibility runner")
    parser.add_argument("--base-path", default=None)
    parser.add_argument("--workdir", default="ml_pipeline/artifacts/t24_phase2_reproducibility")
    parser.add_argument(
        "--phase1-artifacts-dir",
        default=None,
        help="Optional directory containing T13 artifacts (t04/t06/t08/t11).",
    )
    parser.add_argument(
        "--bootstrap-phase1",
        action="store_true",
        help="Bootstrap phase1 prerequisites using reproducibility_runner --single-run inside workdir.",
    )
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t24_phase2_reproducibility_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t24_phase2_reproducibility_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    base = resolve_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: archive base path not found")
        return 2

    root = Path.cwd()
    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    phase1_artifacts, phase1_meta = _resolve_phase1_artifacts_dir(
        base_path=base,
        root=root,
        workdir=workdir,
        phase1_artifacts_dir=args.phase1_artifacts_dir,
        bootstrap_phase1=bool(args.bootstrap_phase1),
    )
    run1 = workdir / "run1"
    run2 = workdir / "run2"

    run1_meta = _run_phase2_once(base_path=base, root=root, run_dir=run1, phase1_artifacts=phase1_artifacts)
    run2_meta = _run_phase2_once(base_path=base, root=root, run_dir=run2, phase1_artifacts=phase1_artifacts)

    artifacts = [
        "artifacts/t14_exit_policy_validation_report.json",
        "artifacts/t14_exit_policy_config.json",
        "artifacts/t05_labeled_features.parquet",
        "artifacts/t05_label_report.json",
        "artifacts/t15_label_path_report.json",
        "artifacts/t16_backtest_trades.parquet",
        "artifacts/t16_backtest_report.json",
        "artifacts/t17_dynamic_exit_policy_report.json",
        "artifacts/t18_exit_policy_optimization_report.json",
        "artifacts/t19_strategy_comparison_v2_report.json",
        "artifacts/t20_backtest_trades.parquet",
        "artifacts/t20_backtest_report.json",
        "artifacts/t21_replay_evaluation_trades.parquet",
        "artifacts/t21_replay_evaluation_report.json",
        "artifacts/t22_exit_aware_paper_events.jsonl",
        "artifacts/t23_execution_monitoring_report.json",
        "artifacts/t23_execution_monitoring_summary.md",
    ]
    comparison = compare_artifact_sets(run1_dir=run1, run2_dir=run2, artifacts=artifacts)
    status = "pass" if comparison.get("mismatch_count", 1) == 0 else "fail"

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "base_path": str(base),
        "phase1_artifacts": phase1_meta,
        "workdir": str(workdir),
        "run1_dir": str(run1),
        "run2_dir": str(run2),
        "run1": run1_meta,
        "run2": run2_meta,
        "comparison": comparison,
    }

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_out.write_text(_summary_markdown(report), encoding="utf-8")

    print(f"Status: {status}")
    print(f"Mismatches: {comparison['mismatch_count']}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
