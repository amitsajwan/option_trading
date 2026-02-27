import argparse
import hashlib
import json
import math
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from .backtest_engine import run_backtest
from .config import DecisionConfig, LabelConfig, TrainConfig
from .dataset_builder import build_canonical_dataset
from .feature.engineering import build_feature_table
from .label_engine import EffectiveLabelConfig, build_labeled_dataset
from .live_inference_adapter import (
    DecisionThresholds,
    predict_probabilities_from_frame,
    infer_action,
    load_model_package,
    run_replay_dry_run,
)
from .monitoring_drift import DriftThresholds, run_drift_assessment
from .quality_profiler import profile_days
from .schema_validator import DEFAULT_REPRESENTATIVE_DAYS, resolve_archive_base, validate_days
from .strategy_comparison import run_strategy_comparison
from .threshold_optimization import run_threshold_optimization
from .train_baseline import save_training_artifacts, train_baseline_models
from .walk_forward import run_walk_forward


VOLATILE_KEYS = {
    "created_at_utc",
    "created_at_ist",
    "generated_at",
    "reference_events_path",
    "current_events_path",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_days(days_raw: Optional[str]) -> List[str]:
    if not days_raw:
        return list(DEFAULT_REPRESENTATIVE_DAYS)
    return [x.strip() for x in str(days_raw).split(",") if x.strip()]


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _jsonl_rows(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def normalize_for_compare(value: object) -> object:
    if isinstance(value, dict):
        out: Dict[str, object] = {}
        for key in sorted(value.keys()):
            if key in VOLATILE_KEYS:
                continue
            out[key] = normalize_for_compare(value[key])
        return out
    if isinstance(value, list):
        return [normalize_for_compare(x) for x in value]
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, np.floating):
        return normalize_for_compare(float(value))
    if isinstance(value, np.integer):
        return int(value)
    return value


def _json_signature(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    normalized = normalize_for_compare(payload)
    text = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonl_signature(path: Path) -> str:
    rows = [normalize_for_compare(row) for row in _jsonl_rows(path)]
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parquet_signature(path: Path) -> str:
    frame = pd.read_parquet(path)
    hashes = pd.util.hash_pandas_object(frame, index=True).to_numpy(dtype="uint64")
    payload = {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "dtypes": [str(x) for x in frame.dtypes.tolist()],
        "hash": hashlib.sha256(hashes.tobytes()).hexdigest(),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_signature(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _json_signature(path)
    if suffix == ".jsonl":
        return _jsonl_signature(path)
    if suffix == ".parquet":
        return _parquet_signature(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_artifact_sets(run1_dir: Path, run2_dir: Path, artifacts: Sequence[str]) -> Dict[str, object]:
    mismatches: List[Dict[str, str]] = []
    compared = 0
    for rel in artifacts:
        a = run1_dir / rel
        b = run2_dir / rel
        if (not a.exists()) or (not b.exists()):
            mismatches.append(
                {
                    "artifact": rel,
                    "reason": "missing",
                    "run1_exists": str(a.exists()),
                    "run2_exists": str(b.exists()),
                }
            )
            continue
        sig_a = artifact_signature(a)
        sig_b = artifact_signature(b)
        compared += 1
        if sig_a != sig_b:
            mismatches.append(
                {
                    "artifact": rel,
                    "reason": "signature_mismatch",
                    "run1_signature": sig_a,
                    "run2_signature": sig_b,
                }
            )
    return {
        "artifacts_checked": int(len(artifacts)),
        "artifacts_compared": int(compared),
        "mismatch_count": int(len(mismatches)),
        "mismatches": mismatches,
        "status": "pass" if len(mismatches) == 0 else "fail",
    }


def _label_summary(df: pd.DataFrame) -> Dict[str, object]:
    ce_valid = (df["ce_label_valid"].fillna(0.0) == 1.0) if "ce_label_valid" in df.columns else pd.Series([], dtype=bool)
    pe_valid = (df["pe_label_valid"].fillna(0.0) == 1.0) if "pe_label_valid" in df.columns else pd.Series([], dtype=bool)
    return {
        "rows_total": int(len(df)),
        "days_total": int(df["trade_date"].nunique()) if "trade_date" in df.columns else 0,
        "ce_valid_rows": int(ce_valid.sum()) if len(ce_valid) else 0,
        "pe_valid_rows": int(pe_valid.sum()) if len(pe_valid) else 0,
        "ce_positive_rate": float(df.loc[ce_valid, "ce_label"].fillna(0.0).mean()) if len(ce_valid) and ce_valid.any() else float("nan"),
        "pe_positive_rate": float(df.loc[pe_valid, "pe_label"].fillna(0.0).mean()) if len(pe_valid) and pe_valid.any() else float("nan"),
    }


def _prediction_table(
    model_package: Dict[str, object],
    feature_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    mode: str = "dual",
) -> pd.DataFrame:
    prob_df, _ = predict_probabilities_from_frame(
        feature_df,
        model_package=model_package,
        context="_prediction_table",
    )
    ce_prob = prob_df["ce_prob"].to_numpy(dtype=float)
    pe_prob = prob_df["pe_prob"].to_numpy(dtype=float)

    actions: List[str] = []
    for ce_p, pe_p in zip(ce_prob, pe_prob):
        actions.append(infer_action(float(ce_p), float(pe_p), ce_threshold, pe_threshold, mode=mode))
    return pd.DataFrame({"ce_prob": ce_prob, "pe_prob": pe_prob, "action": actions})


def _drift_summary_markdown(report: Dict[str, object]) -> str:
    lines: List[str] = []
    lines.append("# Drift Monitoring Summary (T12)")
    lines.append("")
    lines.append(f"- Status: `{report['status']}`")
    lines.append(f"- Generated: `{report['created_at_utc']}`")
    lines.append(f"- Feature count checked: `{report['feature_drift']['feature_count']}`")
    lines.append(f"- CE prob PSI: `{report['prediction_drift'].get('ce_prob_psi')}`")
    lines.append(f"- PE prob PSI: `{report['prediction_drift'].get('pe_prob_psi')}`")
    lines.append(f"- Action share shift max: `{report['prediction_drift'].get('action_share_shift_max')}`")
    lines.append(f"- Alerts: `{len(report['alerts'])}`")
    lines.append("")
    lines.append("## Alerts")
    lines.append("")
    if not report["alerts"]:
        lines.append("- none")
    else:
        for alert in report["alerts"]:
            lines.append(f"- [{alert['severity']}] {alert['message']}")
    return "\n".join(lines) + "\n"


def run_pipeline_once(
    base_path: Path,
    days: Sequence[str],
    out_dir: Path,
    replay_limit: int,
) -> Dict[str, object]:
    artifacts_dir = out_dir / "artifacts"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    steps: Dict[str, Dict[str, object]] = {}

    t0 = time.perf_counter()
    t01_report = validate_days(base_path=base_path, days=days).to_dict()
    t01_path = artifacts_dir / "t01_schema_validation_report.json"
    _write_json(t01_path, t01_report)
    steps["t01"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": int(t01_report["summary"]["fail_count"]) == 0}
    if int(t01_report["summary"]["fail_count"]) > 0:
        raise RuntimeError("T01 schema validation failed in reproducibility run")

    t0 = time.perf_counter()
    t02_report = profile_days(base_path=base_path, days=days)
    t02_json = artifacts_dir / "t02_data_quality_report.json"
    t02_md = artifacts_dir / "t02_data_quality_summary.md"
    _write_json(t02_json, t02_report)
    md_lines = [
        "# Data Quality Summary (T02)",
        "",
        f"- Base path: `{base_path}`",
        f"- Days checked: `{', '.join(days)}`",
        f"- Rows total: `{t02_report['totals']['rows_total']}`",
        f"- Missing cells total: `{t02_report['totals']['missing_cells_total']}`",
        f"- Duplicates total: `{t02_report['totals']['duplicates_total']}`",
        f"- Outliers total: `{t02_report['totals']['outliers_total']}`",
    ]
    _write_markdown(t02_md, "\n".join(md_lines) + "\n")
    steps["t02"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    t0 = time.perf_counter()
    panel = build_canonical_dataset(base_path=base_path, days=days)
    t03_path = artifacts_dir / "t03_canonical_panel.parquet"
    panel.to_parquet(t03_path, index=False)
    steps["t03"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": len(panel) > 0, "rows": int(len(panel))}
    if len(panel) == 0:
        raise RuntimeError("T03 canonical dataset is empty")

    t0 = time.perf_counter()
    features = build_feature_table(panel)
    t04_path = artifacts_dir / "t04_features.parquet"
    features.to_parquet(t04_path, index=False)
    steps["t04"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": len(features) > 0, "rows": int(len(features))}
    if len(features) == 0:
        raise RuntimeError("T04 feature table is empty")

    t0 = time.perf_counter()
    label_cfg = LabelConfig()
    effective = EffectiveLabelConfig(
        horizon_minutes=label_cfg.horizon_minutes,
        return_threshold=label_cfg.return_threshold,
        use_excursion_gate=label_cfg.use_excursion_gate,
        min_favorable_excursion=label_cfg.min_favorable_excursion,
        max_adverse_excursion=label_cfg.max_adverse_excursion,
    )
    labeled = build_labeled_dataset(features=features, base_path=base_path, cfg=effective)
    t05_path = artifacts_dir / "t05_labeled_features.parquet"
    t05_report_path = artifacts_dir / "t05_label_report.json"
    labeled.to_parquet(t05_path, index=False)
    _write_json(t05_report_path, {"base_path": str(base_path), "config": asdict(effective), **_label_summary(labeled)})
    steps["t05"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": len(labeled) > 0, "rows": int(len(labeled))}
    if len(labeled) == 0:
        raise RuntimeError("T05 labeled table is empty")

    t0 = time.perf_counter()
    train_cfg = TrainConfig()
    train_report, models = train_baseline_models(labeled_df=labeled, config=train_cfg)
    t06_model = artifacts_dir / "t06_baseline_model.joblib"
    t06_report = artifacts_dir / "t06_train_report.json"
    save_training_artifacts(train_report, models, model_out=t06_model, report_out=t06_report)
    steps["t06"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    t0 = time.perf_counter()
    wf_cfg = {"train_days": 3, "valid_days": 1, "test_days": 1, "step_days": 1}
    t07_report = run_walk_forward(
        labeled_df=labeled,
        config=train_cfg,
        train_days=wf_cfg["train_days"],
        valid_days=wf_cfg["valid_days"],
        test_days=wf_cfg["test_days"],
        step_days=wf_cfg["step_days"],
    )
    t07_path = artifacts_dir / "t07_walk_forward_report.json"
    _write_json(t07_path, t07_report)
    steps["t07"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    t0 = time.perf_counter()
    decision_cfg = DecisionConfig()
    t08_report = run_threshold_optimization(
        labeled_df=labeled,
        train_config=train_cfg,
        decision_config=decision_cfg,
        train_days=wf_cfg["train_days"],
        valid_days=wf_cfg["valid_days"],
        test_days=wf_cfg["test_days"],
        step_days=wf_cfg["step_days"],
    )
    t08_path = artifacts_dir / "t08_threshold_report.json"
    _write_json(t08_path, t08_report)
    steps["t08"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    ce_threshold = float(t08_report.get("ce", {}).get("selected_threshold", 0.5))
    pe_threshold = float(t08_report.get("pe", {}).get("selected_threshold", 0.5))
    cost_per_trade = float(t08_report.get("decision_config", {}).get("cost_per_trade", decision_cfg.cost_per_trade))

    t0 = time.perf_counter()
    t09_trades, t09_report = run_backtest(
        labeled_df=labeled,
        ce_threshold=ce_threshold,
        pe_threshold=pe_threshold,
        cost_per_trade=cost_per_trade,
        train_config=train_cfg,
        train_days=wf_cfg["train_days"],
        valid_days=wf_cfg["valid_days"],
        test_days=wf_cfg["test_days"],
        step_days=wf_cfg["step_days"],
    )
    t09_trades_path = artifacts_dir / "t09_backtest_trades.parquet"
    t09_report_path = artifacts_dir / "t09_backtest_report.json"
    t09_trades.to_parquet(t09_trades_path, index=False)
    _write_json(t09_report_path, t09_report)
    steps["t09"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    t0 = time.perf_counter()
    t10_report = run_strategy_comparison(
        labeled_df=labeled,
        ce_threshold=ce_threshold,
        pe_threshold=pe_threshold,
        cost_values=[cost_per_trade, 0.001, 0.002],
        train_config=train_cfg,
        train_days=wf_cfg["train_days"],
        valid_days=wf_cfg["valid_days"],
        test_days=wf_cfg["test_days"],
        step_days=wf_cfg["step_days"],
    )
    t10_path = artifacts_dir / "t10_strategy_comparison_report.json"
    _write_json(t10_path, t10_report)
    steps["t10"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    t0 = time.perf_counter()
    t11_path = artifacts_dir / "t11_paper_decisions.jsonl"
    if t11_path.exists():
        t11_path.unlink()
    _ = run_replay_dry_run(
        feature_parquet=t04_path,
        model_package=load_model_package(t06_model),
        thresholds=DecisionThresholds(ce=ce_threshold, pe=pe_threshold, cost_per_trade=cost_per_trade),
        output_jsonl=t11_path,
        mode="dual",
        limit=int(replay_limit),
    )
    steps["t11"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": t11_path.exists()}

    t0 = time.perf_counter()
    model_package = load_model_package(t06_model)
    ref_features = features.head(min(1000, len(features))).copy()
    cur_features = features.tail(min(1000, len(features))).copy()
    reference_pred = _prediction_table(
        model_package=model_package,
        feature_df=ref_features,
        ce_threshold=ce_threshold,
        pe_threshold=pe_threshold,
        mode="dual",
    )
    current_pred = pd.DataFrame(_jsonl_rows(t11_path))
    current_pred = current_pred.loc[:, ["ce_prob", "pe_prob", "action"]]
    t12_report = run_drift_assessment(
        reference_features=ref_features,
        current_features=cur_features,
        reference_predictions=reference_pred,
        current_predictions=current_pred,
        thresholds=DriftThresholds(),
    )
    t12_json = artifacts_dir / "t12_drift_report.json"
    t12_md = artifacts_dir / "t12_drift_summary.md"
    _write_json(t12_json, t12_report)
    _write_markdown(t12_md, _drift_summary_markdown(t12_report))
    steps["t12"] = {"seconds": round(time.perf_counter() - t0, 3), "ok": True}

    return {
        "out_dir": str(out_dir),
        "artifacts_dir": str(artifacts_dir),
        "steps": steps,
        "artifacts": {
            "t01": str(t01_path),
            "t02_json": str(t02_json),
            "t02_md": str(t02_md),
            "t03": str(t03_path),
            "t04": str(t04_path),
            "t05": str(t05_path),
            "t05_report": str(t05_report_path),
            "t06_model": str(t06_model),
            "t06_report": str(t06_report),
            "t07": str(t07_path),
            "t08": str(t08_path),
            "t09_trades": str(t09_trades_path),
            "t09_report": str(t09_report_path),
            "t10": str(t10_path),
            "t11": str(t11_path),
            "t12_json": str(t12_json),
            "t12_md": str(t12_md),
        },
    }


def _repro_summary_markdown(report: Dict[str, object]) -> str:
    lines: List[str] = []
    lines.append("# Reproducibility Summary (T13)")
    lines.append("")
    lines.append(f"- Status: `{report['status']}`")
    lines.append(f"- Generated: `{report['created_at_utc']}`")
    lines.append(f"- Base path: `{report['base_path']}`")
    lines.append(f"- Days: `{', '.join(report['days'])}`")
    lines.append(f"- Run1 dir: `{report['run1_dir']}`")
    lines.append(f"- Run2 dir: `{report.get('run2_dir')}`")
    cmp = report.get("comparison")
    if cmp is not None:
        lines.append(f"- Artifacts compared: `{cmp['artifacts_compared']}` / `{cmp['artifacts_checked']}`")
        lines.append(f"- Mismatches: `{cmp['mismatch_count']}`")
    lines.append("")
    lines.append("## Step Timing (Run1)")
    lines.append("")
    for step, payload in report["run1"]["steps"].items():
        lines.append(f"- `{step}`: {payload['seconds']}s")
    if report.get("run2"):
        lines.append("")
        lines.append("## Step Timing (Run2)")
        lines.append("")
        for step, payload in report["run2"]["steps"].items():
            lines.append(f"- `{step}`: {payload['seconds']}s")
    lines.append("")
    lines.append("## Comparison")
    lines.append("")
    if cmp is None:
        lines.append("- not_run (`single_run=true`)")
    elif cmp.get("mismatch_count", 0) == 0:
        lines.append("- pass")
    else:
        for mm in cmp["mismatches"]:
            lines.append(f"- {mm['artifact']}: {mm['reason']}")
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run clean-room reproducibility flow for T13")
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument("--days", default=None, help="Comma-separated day list (YYYY-MM-DD)")
    parser.add_argument("--workdir", default="ml_pipeline/artifacts/t13_reproducibility")
    parser.add_argument("--replay-limit", type=int, default=200)
    parser.add_argument("--single-run", action="store_true", help="Skip second run and comparison")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t13_reproducibility_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t13_reproducibility_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    base_path = resolve_archive_base(explicit_base=args.base_path)
    if base_path is None:
        print("ERROR: archive base path not found")
        return 2
    days = _parse_days(args.days)
    if not days:
        print("ERROR: no days selected")
        return 2

    workdir = Path(args.workdir)
    run1_dir = workdir / "run1"
    run2_dir = workdir / "run2"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    run1 = run_pipeline_once(base_path=base_path, days=days, out_dir=run1_dir, replay_limit=int(args.replay_limit))
    run2 = None
    comparison = None
    status = "pass"
    if not args.single_run:
        run2 = run_pipeline_once(base_path=base_path, days=days, out_dir=run2_dir, replay_limit=int(args.replay_limit))
        artifacts_to_compare = [
            "artifacts/t01_schema_validation_report.json",
            "artifacts/t02_data_quality_report.json",
            "artifacts/t03_canonical_panel.parquet",
            "artifacts/t04_features.parquet",
            "artifacts/t05_labeled_features.parquet",
            "artifacts/t05_label_report.json",
            "artifacts/t06_train_report.json",
            "artifacts/t07_walk_forward_report.json",
            "artifacts/t08_threshold_report.json",
            "artifacts/t09_backtest_trades.parquet",
            "artifacts/t09_backtest_report.json",
            "artifacts/t10_strategy_comparison_report.json",
            "artifacts/t11_paper_decisions.jsonl",
            "artifacts/t12_drift_report.json",
        ]
        comparison = compare_artifact_sets(run1_dir=run1_dir, run2_dir=run2_dir, artifacts=artifacts_to_compare)
        status = "pass" if comparison.get("mismatch_count", 1) == 0 else "fail"

    report = {
        "created_at_utc": _utc_now(),
        "status": status,
        "base_path": str(base_path),
        "days": [str(d) for d in days],
        "workdir": str(workdir),
        "run1_dir": str(run1_dir),
        "run2_dir": str(run2_dir) if run2 else None,
        "single_run": bool(args.single_run),
        "replay_limit": int(args.replay_limit),
        "run1": run1,
        "run2": run2,
        "comparison": comparison,
    }

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    _write_json(report_out, report)
    _write_markdown(summary_out, _repro_summary_markdown(report))

    print(f"Status: {status}")
    if comparison:
        print(f"Mismatches: {comparison['mismatch_count']}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())

