import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .config import TrainConfig
from .train_baseline import FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, FEATURE_PROFILES, select_feature_columns
from .walk_forward import run_walk_forward


IST = timezone(timedelta(hours=5, minutes=30))


SUSPICIOUS_PATTERNS: Sequence[str] = (
    r"label",
    r"forward_return",
    r"entry_price",
    r"exit_price",
    r"\bmfe\b",
    r"\bmae\b",
    r"path_",
    r"tp_",
    r"sl_",
    r"hold_extension",
    r"best_side",
)


def detect_suspicious_features(feature_columns: Sequence[str]) -> List[str]:
    bad: List[str] = []
    for col in feature_columns:
        low = str(col).lower()
        if any(re.search(pat, low) for pat in SUSPICIOUS_PATTERNS):
            bad.append(str(col))
    return sorted(bad)


def _side_df(labeled_df: pd.DataFrame, side: str) -> pd.DataFrame:
    target_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    out = labeled_df[(labeled_df[valid_col] == 1.0) & labeled_df[target_col].notna()].copy()
    out[target_col] = out[target_col].astype(int)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def _simple_auc(train_df: pd.DataFrame, test_df: pd.DataFrame, feature_cols: Sequence[str], target_col: str) -> Optional[float]:
    if len(train_df) == 0 or len(test_df) == 0:
        return None
    y_train = train_df[target_col].astype(int).to_numpy()
    y_test = test_df[target_col].astype(int).to_numpy()
    if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
        return None
    pipe = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler(with_mean=True, with_std=True)),
            ("model", LogisticRegression(max_iter=1000, random_state=42)),
        ]
    )
    pipe.fit(train_df.loc[:, list(feature_cols)], y_train)
    prob = pipe.predict_proba(test_df.loc[:, list(feature_cols)])[:, 1]
    return float(roc_auc_score(y_test, prob))


def synthetic_leakage_injection_check(
    labeled_df: pd.DataFrame,
    side: str,
    feature_profile: str = FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    min_auc_lift: float = 0.20,
) -> Dict[str, object]:
    work = _side_df(labeled_df, side=side)
    target_col = f"{side}_label"
    features = select_feature_columns(work, feature_profile=feature_profile)
    if not features:
        raise ValueError("no features for leakage injection check")

    split = int(np.floor(len(work) * 0.7))
    split = max(20, min(split, len(work) - 20))
    train_df = work.iloc[:split].copy()
    test_df = work.iloc[split:].copy()

    baseline_auc = _simple_auc(train_df, test_df, features, target_col=target_col)

    # Synthetic leak: direct target copy (must produce near-perfect separability).
    leak_col = "synthetic_leak_target_copy"
    train_leak = train_df.copy()
    test_leak = test_df.copy()
    train_leak[leak_col] = train_leak[target_col].astype(float)
    test_leak[leak_col] = test_leak[target_col].astype(float)
    injected_auc = _simple_auc(train_leak, test_leak, list(features) + [leak_col], target_col=target_col)

    detected = False
    if injected_auc is not None:
        if baseline_auc is None:
            detected = injected_auc >= 0.95
        else:
            lift = injected_auc - baseline_auc
            # If baseline is already near-perfect on synthetic data, treat near-perfect injected AUC as detected.
            detected = (injected_auc >= 0.95) and ((lift >= float(min_auc_lift)) or (baseline_auc >= 0.94))

    return {
        "side": side.upper(),
        "rows_total": int(len(work)),
        "split_rows": {"train": int(len(train_df)), "test": int(len(test_df))},
        "baseline_auc": baseline_auc,
        "injected_auc": injected_auc,
        "auc_lift": (float(injected_auc - baseline_auc) if baseline_auc is not None and injected_auc is not None else None),
        "detected": bool(detected),
        "min_auc_lift": float(min_auc_lift),
    }


def run_leakage_audit(
    labeled_df: pd.DataFrame,
    feature_profile: str = FEATURE_PROFILE_FUTURES_OPTIONS_ONLY,
    train_days: int = 180,
    valid_days: int = 30,
    test_days: int = 30,
    step_days: int = 30,
    purge_days: int = 1,
    embargo_days: int = 1,
) -> Dict[str, object]:
    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    feature_cols = select_feature_columns(frame, feature_profile=feature_profile)
    suspicious = detect_suspicious_features(feature_cols)

    cfg = TrainConfig()
    wf_report = run_walk_forward(
        labeled_df=frame,
        config=cfg,
        train_days=int(train_days),
        valid_days=int(valid_days),
        test_days=int(test_days),
        step_days=int(step_days),
        purge_days=int(purge_days),
        embargo_days=int(embargo_days),
        feature_profile=feature_profile,
    )

    ce_leak = synthetic_leakage_injection_check(frame, side="ce", feature_profile=feature_profile)
    pe_leak = synthetic_leakage_injection_check(frame, side="pe", feature_profile=feature_profile)
    injection_ok = bool(ce_leak["detected"]) and bool(pe_leak["detected"])

    report = {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T28",
        "status": "completed",
        "rows_total": int(len(frame)),
        "days_total": int(frame["trade_date"].astype(str).nunique()) if "trade_date" in frame.columns else 0,
        "feature_profile": str(feature_profile),
        "feature_count": int(len(feature_cols)),
        "suspicious_feature_names": suspicious,
        "suspicious_feature_check_passed": len(suspicious) == 0,
        "synthetic_leakage_injection": {
            "ce": ce_leak,
            "pe": pe_leak,
            "both_sides_detected": injection_ok,
        },
        "purged_walk_forward": wf_report,
        "overall_passed": (len(suspicious) == 0) and injection_ok,
    }
    return report


def _summary_md(report: Dict[str, object]) -> str:
    inj = report["synthetic_leakage_injection"]
    lines = [
        "# T28 Leakage Audit Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Rows: `{report['rows_total']}` over `{report['days_total']}` days",
        f"- Feature profile: `{report['feature_profile']}` (`{report['feature_count']}` features)",
        f"- Suspicious feature check: `{report['suspicious_feature_check_passed']}`",
        f"- Synthetic leakage detection (both sides): `{inj['both_sides_detected']}`",
        f"- Overall passed: `{report['overall_passed']}`",
        "",
        "## Synthetic Injection",
        f"- CE baseline/injected AUC: `{inj['ce']['baseline_auc']}` -> `{inj['ce']['injected_auc']}`",
        f"- PE baseline/injected AUC: `{inj['pe']['baseline_auc']}` -> `{inj['pe']['injected_auc']}`",
        "",
        "## Purged Walk-Forward",
        f"- CE folds: `{report['purged_walk_forward']['ce']['fold_count']}`",
        f"- PE folds: `{report['purged_walk_forward']['pe']['fold_count']}`",
        f"- Purge days: `{report['purged_walk_forward']['walk_forward_config']['purge_days']}`",
        f"- Embargo days: `{report['purged_walk_forward']['walk_forward_config']['embargo_days']}`",
    ]
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T28 leakage audit + purged walk-forward")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t29_2y_auto_t05_labeled_features.parquet")
    parser.add_argument("--feature-profile", default=FEATURE_PROFILE_FUTURES_OPTIONS_ONLY, choices=list(FEATURE_PROFILES))
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--valid-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--purge-days", type=int, default=1)
    parser.add_argument("--embargo-days", type=int, default=1)
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t28_leakage_audit_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t28_leakage_audit_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2
    labeled = pd.read_parquet(labeled_path)
    report = run_leakage_audit(
        labeled_df=labeled,
        feature_profile=str(args.feature_profile),
        train_days=int(args.train_days),
        valid_days=int(args.valid_days),
        test_days=int(args.test_days),
        step_days=int(args.step_days),
        purge_days=int(args.purge_days),
        embargo_days=int(args.embargo_days),
    )

    report_path = Path(args.report_out)
    summary_path = Path(args.summary_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_md(report), encoding="utf-8")

    print(f"Rows: {report['rows_total']}")
    print(f"Features: {report['feature_count']}")
    print(f"Suspicious feature check passed: {report['suspicious_feature_check_passed']}")
    print(f"Synthetic leakage detected on both sides: {report['synthetic_leakage_injection']['both_sides_detected']}")
    print(f"Overall passed: {report['overall_passed']}")
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
