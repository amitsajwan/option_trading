import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExecutionDriftThresholds:
    event_share_shift_alert: float = 0.15
    exit_reason_shift_alert: float = 0.15
    hold_mean_shift_alert: float = 2.0
    hold_p95_shift_alert: float = 3.0


def _load_jsonl(path: Path) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    if not rows:
        return pd.DataFrame(columns=["event_type", "event_reason", "held_minutes"])
    df = pd.DataFrame(rows)
    if "event_type" not in df.columns:
        df["event_type"] = "UNKNOWN"
    if "event_reason" not in df.columns:
        df["event_reason"] = "unknown"
    if "held_minutes" not in df.columns:
        df["held_minutes"] = np.nan
    df["event_type"] = df["event_type"].astype(str)
    df["event_reason"] = df["event_reason"].astype(str)
    df["held_minutes"] = pd.to_numeric(df["held_minutes"], errors="coerce")
    return df


def _share_distribution(series: pd.Series) -> Dict[str, float]:
    if len(series) == 0:
        return {}
    vals = series.astype(str).value_counts(normalize=True).to_dict()
    return {str(k): float(v) for k, v in vals.items()}


def _max_share_shift(reference: Dict[str, float], current: Dict[str, float]) -> float:
    keys = set(reference.keys()) | set(current.keys())
    if not keys:
        return 0.0
    return float(max(abs(float(current.get(k, 0.0)) - float(reference.get(k, 0.0))) for k in keys))


def _hold_stats(df: pd.DataFrame) -> Dict[str, float]:
    series = pd.to_numeric(df["held_minutes"], errors="coerce").dropna()
    if len(series) == 0:
        return {"count": 0.0, "mean": 0.0, "p95": 0.0}
    return {
        "count": float(len(series)),
        "mean": float(series.mean()),
        "p95": float(series.quantile(0.95)),
    }


def evaluate_execution_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    thresholds: ExecutionDriftThresholds,
) -> Dict[str, object]:
    ref_event = _share_distribution(reference_df["event_type"])
    cur_event = _share_distribution(current_df["event_type"])
    event_shift = _max_share_shift(ref_event, cur_event)

    ref_exit = _share_distribution(reference_df[reference_df["event_type"] == "EXIT"]["event_reason"])
    cur_exit = _share_distribution(current_df[current_df["event_type"] == "EXIT"]["event_reason"])
    exit_shift = _max_share_shift(ref_exit, cur_exit)

    ref_hold = _hold_stats(reference_df[reference_df["event_type"] == "EXIT"])
    cur_hold = _hold_stats(current_df[current_df["event_type"] == "EXIT"])
    hold_mean_shift = float(abs(cur_hold["mean"] - ref_hold["mean"]))
    hold_p95_shift = float(abs(cur_hold["p95"] - ref_hold["p95"]))

    alerts: List[Dict[str, object]] = []
    if event_shift >= thresholds.event_share_shift_alert:
        alerts.append(
            {
                "type": "event_mix_drift",
                "severity": "high",
                "value": event_shift,
                "message": f"Event share shift {event_shift:.4f} exceeds threshold",
            }
        )
    if exit_shift >= thresholds.exit_reason_shift_alert:
        alerts.append(
            {
                "type": "exit_reason_drift",
                "severity": "high",
                "value": exit_shift,
                "message": f"Exit reason share shift {exit_shift:.4f} exceeds threshold",
            }
        )
    if hold_mean_shift >= thresholds.hold_mean_shift_alert:
        alerts.append(
            {
                "type": "hold_duration_drift",
                "severity": "high",
                "value": hold_mean_shift,
                "message": f"Hold mean shift {hold_mean_shift:.4f} exceeds threshold",
            }
        )
    if hold_p95_shift >= thresholds.hold_p95_shift_alert:
        alerts.append(
            {
                "type": "hold_duration_tail_drift",
                "severity": "high",
                "value": hold_p95_shift,
                "message": f"Hold p95 shift {hold_p95_shift:.4f} exceeds threshold",
            }
        )

    status = "alert" if alerts else "ok"
    return {
        "status": status,
        "thresholds": asdict(thresholds),
        "event_type_distribution": {
            "reference": ref_event,
            "current": cur_event,
            "max_shift": event_shift,
        },
        "exit_reason_distribution": {
            "reference": ref_exit,
            "current": cur_exit,
            "max_shift": exit_shift,
        },
        "hold_duration": {
            "reference": ref_hold,
            "current": cur_hold,
            "mean_shift": hold_mean_shift,
            "p95_shift": hold_p95_shift,
        },
        "alerts": alerts,
    }


def _summary_markdown(report: Dict[str, object]) -> str:
    lines: List[str] = []
    lines.append("# Execution Monitoring Summary (T23)")
    lines.append("")
    lines.append(f"- Status: `{report['status']}`")
    lines.append(f"- Event share max shift: `{report['event_type_distribution']['max_shift']}`")
    lines.append(f"- Exit reason max shift: `{report['exit_reason_distribution']['max_shift']}`")
    lines.append(f"- Hold mean shift: `{report['hold_duration']['mean_shift']}`")
    lines.append(f"- Hold p95 shift: `{report['hold_duration']['p95_shift']}`")
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


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Execution quality monitoring and drift checks")
    parser.add_argument("--reference-events", default="ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl")
    parser.add_argument("--current-events", default="ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl")
    parser.add_argument("--event-share-shift-alert", type=float, default=0.15)
    parser.add_argument("--exit-reason-shift-alert", type=float, default=0.15)
    parser.add_argument("--hold-mean-shift-alert", type=float, default=2.0)
    parser.add_argument("--hold-p95-shift-alert", type=float, default=3.0)
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t23_execution_monitoring_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t23_execution_monitoring_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    ref_path = Path(args.reference_events)
    cur_path = Path(args.current_events)
    if not ref_path.exists():
        print(f"ERROR: reference events file not found: {ref_path}")
        return 2
    if not cur_path.exists():
        print(f"ERROR: current events file not found: {cur_path}")
        return 2

    thresholds = ExecutionDriftThresholds(
        event_share_shift_alert=float(args.event_share_shift_alert),
        exit_reason_shift_alert=float(args.exit_reason_shift_alert),
        hold_mean_shift_alert=float(args.hold_mean_shift_alert),
        hold_p95_shift_alert=float(args.hold_p95_shift_alert),
    )
    reference_df = _load_jsonl(ref_path)
    current_df = _load_jsonl(cur_path)
    drift = evaluate_execution_drift(reference_df, current_df, thresholds=thresholds)
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "reference_events_path": str(ref_path),
        "current_events_path": str(cur_path),
        "reference_rows": int(len(reference_df)),
        "current_rows": int(len(current_df)),
        **drift,
    }

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_out.write_text(_summary_markdown(report), encoding="utf-8")

    print(f"Status: {report['status']}")
    print(f"Alerts: {len(report['alerts'])}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
