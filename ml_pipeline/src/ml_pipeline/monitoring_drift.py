import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .live_inference_adapter import load_model_package, predict_probabilities_from_frame


@dataclass(frozen=True)
class DriftThresholds:
    feature_psi_warn: float = 0.10
    feature_psi_alert: float = 0.20
    prediction_psi_alert: float = 0.20
    action_share_alert: float = 0.15


def _safe_series(values: pd.Series) -> pd.Series:
    out = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return out.astype(float)


def compute_psi(reference: pd.Series, current: pd.Series, bins: int = 10) -> float:
    ref = _safe_series(reference)
    cur = _safe_series(current)
    if len(ref) == 0 or len(cur) == 0:
        return float("nan")

    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.unique(np.quantile(ref, quantiles))
    if len(edges) < 3:
        return 0.0

    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)
    ref_ratio = ref_counts / max(ref_counts.sum(), 1)
    cur_ratio = cur_counts / max(cur_counts.sum(), 1)

    eps = 1e-8
    ref_ratio = np.clip(ref_ratio, eps, None)
    cur_ratio = np.clip(cur_ratio, eps, None)
    psi = np.sum((cur_ratio - ref_ratio) * np.log(cur_ratio / ref_ratio))
    return float(psi)


def numeric_feature_columns(df: pd.DataFrame) -> List[str]:
    cols = list(df.select_dtypes(include=[np.number]).columns)
    return cols


def compute_feature_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    feature_columns: Sequence[str],
) -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    for col in feature_columns:
        if col not in reference_df.columns or col not in current_df.columns:
            continue
        ref = _safe_series(reference_df[col])
        cur = _safe_series(current_df[col])
        if len(ref) == 0 or len(cur) == 0:
            continue
        ref_mean = float(ref.mean())
        cur_mean = float(cur.mean())
        ref_std = float(ref.std(ddof=0))
        mean_shift_std = float((cur_mean - ref_mean) / ref_std) if ref_std > 1e-12 else 0.0
        psi = compute_psi(ref, cur, bins=10)
        rows.append(
            {
                "feature": col,
                "reference_mean": ref_mean,
                "current_mean": cur_mean,
                "reference_std": ref_std,
                "mean_shift_std": mean_shift_std,
                "psi": psi,
                "reference_count": int(len(ref)),
                "current_count": int(len(cur)),
            }
        )

    rows.sort(key=lambda x: (float(x["psi"]) if np.isfinite(x["psi"]) else -1.0), reverse=True)
    return {
        "feature_count": int(len(rows)),
        "features": rows,
    }


def _action_share(df: pd.DataFrame) -> Dict[str, float]:
    if len(df) == 0:
        return {"BUY_CE": 0.0, "BUY_PE": 0.0, "HOLD": 0.0}
    counts = df["action"].value_counts(normalize=True).to_dict()
    return {
        "BUY_CE": float(counts.get("BUY_CE", 0.0)),
        "BUY_PE": float(counts.get("BUY_PE", 0.0)),
        "HOLD": float(counts.get("HOLD", 0.0)),
    }


def compute_prediction_drift(reference_pred: pd.DataFrame, current_pred: pd.DataFrame) -> Dict[str, object]:
    ref = reference_pred.copy()
    cur = current_pred.copy()
    for col in ("ce_prob", "pe_prob"):
        ref[col] = pd.to_numeric(ref[col], errors="coerce")
        cur[col] = pd.to_numeric(cur[col], errors="coerce")

    ce_psi = compute_psi(ref["ce_prob"], cur["ce_prob"], bins=10)
    pe_psi = compute_psi(ref["pe_prob"], cur["pe_prob"], bins=10)
    ref_action = _action_share(ref)
    cur_action = _action_share(cur)
    action_shift_max = max(abs(cur_action[k] - ref_action.get(k, 0.0)) for k in cur_action)
    return {
        "reference_rows": int(len(ref)),
        "current_rows": int(len(cur)),
        "ce_prob_psi": ce_psi,
        "pe_prob_psi": pe_psi,
        "reference_action_share": ref_action,
        "current_action_share": cur_action,
        "action_share_shift_max": float(action_shift_max),
        "reference_ce_prob_mean": float(_safe_series(ref["ce_prob"]).mean()) if len(ref) else float("nan"),
        "current_ce_prob_mean": float(_safe_series(cur["ce_prob"]).mean()) if len(cur) else float("nan"),
        "reference_pe_prob_mean": float(_safe_series(ref["pe_prob"]).mean()) if len(ref) else float("nan"),
        "current_pe_prob_mean": float(_safe_series(cur["pe_prob"]).mean()) if len(cur) else float("nan"),
    }


def evaluate_alerts(
    feature_drift: Dict[str, object],
    prediction_drift: Dict[str, object],
    thresholds: DriftThresholds,
) -> List[Dict[str, object]]:
    alerts: List[Dict[str, object]] = []
    for row in feature_drift.get("features", []):
        psi = row.get("psi")
        if psi is None or not np.isfinite(psi):
            continue
        if psi >= thresholds.feature_psi_alert:
            alerts.append(
                {
                    "type": "feature_drift",
                    "severity": "high",
                    "feature": row["feature"],
                    "psi": float(psi),
                    "message": f"Feature {row['feature']} PSI {psi:.4f} exceeds alert threshold",
                }
            )
        elif psi >= thresholds.feature_psi_warn:
            alerts.append(
                {
                    "type": "feature_drift",
                    "severity": "warn",
                    "feature": row["feature"],
                    "psi": float(psi),
                    "message": f"Feature {row['feature']} PSI {psi:.4f} exceeds warn threshold",
                }
            )

    ce_psi = prediction_drift.get("ce_prob_psi")
    pe_psi = prediction_drift.get("pe_prob_psi")
    if ce_psi is not None and np.isfinite(ce_psi) and ce_psi >= thresholds.prediction_psi_alert:
        alerts.append(
            {
                "type": "prediction_drift",
                "severity": "high",
                "channel": "ce_prob",
                "psi": float(ce_psi),
                "message": f"CE probability PSI {ce_psi:.4f} exceeds prediction threshold",
            }
        )
    if pe_psi is not None and np.isfinite(pe_psi) and pe_psi >= thresholds.prediction_psi_alert:
        alerts.append(
            {
                "type": "prediction_drift",
                "severity": "high",
                "channel": "pe_prob",
                "psi": float(pe_psi),
                "message": f"PE probability PSI {pe_psi:.4f} exceeds prediction threshold",
            }
        )

    action_shift = prediction_drift.get("action_share_shift_max")
    if action_shift is not None and np.isfinite(action_shift) and action_shift >= thresholds.action_share_alert:
        alerts.append(
            {
                "type": "prediction_drift",
                "severity": "high",
                "channel": "action_share",
                "value": float(action_shift),
                "message": f"Action share shift {action_shift:.4f} exceeds threshold",
            }
        )
    return alerts


def _load_jsonl(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                rows.append(parsed)
        except json.JSONDecodeError:
            continue
    return rows


def _reference_predictions_from_model(
    model_package: Dict[str, object],
    reference_features: pd.DataFrame,
    mode: str,
    ce_threshold: float,
    pe_threshold: float,
) -> pd.DataFrame:
    prob_df, _ = predict_probabilities_from_frame(
        reference_features,
        model_package=model_package,
        context="_reference_predictions_from_model",
    )
    ce_prob = prob_df["ce_prob"].to_numpy(dtype=float)
    pe_prob = prob_df["pe_prob"].to_numpy(dtype=float)

    actions = []
    for c, p in zip(ce_prob, pe_prob):
        ce_ok = c >= ce_threshold
        pe_ok = p >= pe_threshold
        if mode == "ce_only":
            actions.append("BUY_CE" if ce_ok else "HOLD")
        elif mode == "pe_only":
            actions.append("BUY_PE" if pe_ok else "HOLD")
        else:
            if ce_ok and pe_ok:
                actions.append("BUY_CE" if c >= p else "BUY_PE")
            elif ce_ok:
                actions.append("BUY_CE")
            elif pe_ok:
                actions.append("BUY_PE")
            else:
                actions.append("HOLD")
    return pd.DataFrame({"ce_prob": ce_prob, "pe_prob": pe_prob, "action": actions})


def run_drift_assessment(
    reference_features: pd.DataFrame,
    current_features: pd.DataFrame,
    reference_predictions: pd.DataFrame,
    current_predictions: pd.DataFrame,
    thresholds: DriftThresholds,
) -> Dict[str, object]:
    shared_features = [c for c in numeric_feature_columns(reference_features) if c in current_features.columns]
    feature_drift = compute_feature_drift(reference_features, current_features, feature_columns=shared_features)
    prediction_drift = compute_prediction_drift(reference_predictions, current_predictions)
    alerts = evaluate_alerts(feature_drift, prediction_drift, thresholds=thresholds)
    status = "alert" if any(a["severity"] == "high" for a in alerts) else ("warn" if alerts else "ok")
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "thresholds": asdict(thresholds),
        "feature_drift": feature_drift,
        "prediction_drift": prediction_drift,
        "alerts": alerts,
    }
    return report


def _write_summary(path: Path, report: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    lines.append("## Top Feature PSI")
    lines.append("")
    for row in report["feature_drift"]["features"][:10]:
        lines.append(f"- `{row['feature']}`: psi={row['psi']:.6f}, mean_shift_std={row['mean_shift_std']:.6f}")
    lines.append("")
    lines.append("## Alerts")
    lines.append("")
    if not report["alerts"]:
        lines.append("- none")
    else:
        for alert in report["alerts"]:
            lines.append(f"- [{alert['severity']}] {alert['message']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Monitoring and drift checks")
    parser.add_argument("--model-package", default="ml_pipeline/artifacts/t06_baseline_model.joblib")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t08_threshold_report.json")
    parser.add_argument("--reference-features", default="ml_pipeline/artifacts/t04_features.parquet")
    parser.add_argument("--current-features", default="ml_pipeline/artifacts/t04_features.parquet")
    parser.add_argument("--current-decisions", default="ml_pipeline/artifacts/t11_paper_decisions.jsonl")
    parser.add_argument("--mode", default="dual", choices=["dual", "ce_only", "pe_only"])
    parser.add_argument("--reference-limit", type=int, default=1000)
    parser.add_argument("--current-limit", type=int, default=1000)
    parser.add_argument("--feature-psi-warn", type=float, default=0.10)
    parser.add_argument("--feature-psi-alert", type=float, default=0.20)
    parser.add_argument("--prediction-psi-alert", type=float, default=0.20)
    parser.add_argument("--action-share-alert", type=float, default=0.15)
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t12_drift_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t12_drift_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    model_path = Path(args.model_package)
    threshold_path = Path(args.threshold_report)
    ref_feat_path = Path(args.reference_features)
    cur_feat_path = Path(args.current_features)
    cur_decisions_path = Path(args.current_decisions)
    for p in (model_path, threshold_path, ref_feat_path, cur_feat_path, cur_decisions_path):
        if not p.exists():
            print(f"ERROR: missing input file: {p}")
            return 2

    model_package = load_model_package(model_path)
    threshold_payload = json.loads(threshold_path.read_text(encoding="utf-8"))
    ce_thr = float(threshold_payload.get("ce", {}).get("selected_threshold", 0.5))
    pe_thr = float(threshold_payload.get("pe", {}).get("selected_threshold", 0.5))

    ref_feat = pd.read_parquet(ref_feat_path)
    cur_feat = pd.read_parquet(cur_feat_path)
    ref_feat = ref_feat.head(int(args.reference_limit)).copy()
    cur_feat = cur_feat.head(int(args.current_limit)).copy()
    reference_pred = _reference_predictions_from_model(
        model_package=model_package,
        reference_features=ref_feat,
        mode=args.mode,
        ce_threshold=ce_thr,
        pe_threshold=pe_thr,
    )
    current_decision_rows = _load_jsonl(cur_decisions_path)
    current_pred = pd.DataFrame(current_decision_rows)
    needed = {"ce_prob", "pe_prob", "action"}
    if not needed.issubset(set(current_pred.columns)):
        print("ERROR: current decisions jsonl missing required keys ce_prob/pe_prob/action")
        return 2

    thresholds = DriftThresholds(
        feature_psi_warn=float(args.feature_psi_warn),
        feature_psi_alert=float(args.feature_psi_alert),
        prediction_psi_alert=float(args.prediction_psi_alert),
        action_share_alert=float(args.action_share_alert),
    )
    report = run_drift_assessment(
        reference_features=ref_feat,
        current_features=cur_feat,
        reference_predictions=reference_pred,
        current_predictions=current_pred,
        thresholds=thresholds,
    )

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _write_summary(summary_out, report)

    print(f"Status: {report['status']}")
    print(f"Alerts: {len(report['alerts'])}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
