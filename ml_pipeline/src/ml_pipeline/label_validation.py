import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import numpy as np
import pandas as pd


IST = timezone(timedelta(hours=5, minutes=30))


def compute_forward_return_by_day(df: pd.DataFrame, horizon_minutes: int) -> pd.Series:
    if "trade_date" not in df.columns or "fut_close" not in df.columns:
        raise ValueError("required columns missing: trade_date, fut_close")
    out = df.copy()
    out["trade_date"] = out["trade_date"].astype(str)
    out["fut_close"] = pd.to_numeric(out["fut_close"], errors="coerce")
    future_close = out.groupby("trade_date", sort=False)["fut_close"].shift(-int(horizon_minutes))
    return (future_close - out["fut_close"]) / out["fut_close"]


def build_breakout_alternative_labels(
    labeled_df: pd.DataFrame,
    horizon_minutes: int = 3,
    return_threshold: float = 0.002,
) -> pd.DataFrame:
    required = {"timestamp", "trade_date", "fut_close", "opening_range_ready", "opening_range_breakout_up", "opening_range_breakout_down"}
    missing = sorted(list(required - set(labeled_df.columns)))
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    frame["fut_forward_return_h"] = compute_forward_return_by_day(frame, horizon_minutes=horizon_minutes)
    frame["breakout_label_valid"] = frame["fut_forward_return_h"].notna().astype(float)

    ready = frame["opening_range_ready"].fillna(0.0).astype(float) == 1.0
    up = frame["opening_range_breakout_up"].fillna(0.0).astype(float) == 1.0
    down = frame["opening_range_breakout_down"].fillna(0.0).astype(float) == 1.0
    ce_candidate = (ready & up)
    pe_candidate = (ready & down)

    thr = float(return_threshold)
    valid = frame["breakout_label_valid"] == 1.0
    ce_positive = ce_candidate & (frame["fut_forward_return_h"] >= thr)
    pe_positive = pe_candidate & (frame["fut_forward_return_h"] <= -thr)

    frame["ce_breakout_candidate"] = ce_candidate.astype(float)
    frame["pe_breakout_candidate"] = pe_candidate.astype(float)
    frame["ce_breakout_label"] = np.where(valid, ce_positive.astype(float), np.nan)
    frame["pe_breakout_label"] = np.where(valid, pe_positive.astype(float), np.nan)
    frame["breakout_no_trade"] = np.where(valid, ((ce_candidate | pe_candidate) == 0).astype(float), np.nan)
    frame["breakout_horizon_minutes"] = int(horizon_minutes)
    frame["breakout_return_threshold"] = float(return_threshold)

    keep_cols = [
        "timestamp",
        "trade_date",
        "fut_close",
        "fut_forward_return_h",
        "breakout_label_valid",
        "ce_breakout_candidate",
        "pe_breakout_candidate",
        "ce_breakout_label",
        "pe_breakout_label",
        "breakout_no_trade",
        "breakout_horizon_minutes",
        "breakout_return_threshold",
    ]
    return frame.loc[:, keep_cols].copy()


def _rate(series: pd.Series) -> Optional[float]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) == 0:
        return None
    return float(s.mean())


def _agreement(base: pd.Series, alt: pd.Series, mask: pd.Series) -> Optional[float]:
    m = mask.fillna(False)
    if int(m.sum()) == 0:
        return None
    a = pd.to_numeric(base[m], errors="coerce")
    b = pd.to_numeric(alt[m], errors="coerce")
    both = a.notna() & b.notna()
    if int(both.sum()) == 0:
        return None
    return float((a[both].astype(int) == b[both].astype(int)).mean())


def run_label_horizon_validation(
    labeled_df: pd.DataFrame,
    horizon_minutes: int = 3,
    return_threshold: float = 0.002,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    frame = labeled_df.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    alt = build_breakout_alternative_labels(
        labeled_df=frame,
        horizon_minutes=horizon_minutes,
        return_threshold=return_threshold,
    )
    merged = frame.copy()
    alt_cols = [c for c in alt.columns if c not in {"timestamp", "trade_date", "fut_close"}]
    if len(alt) != len(merged):
        raise ValueError("alternative label rows do not align with base frame")
    for col in alt_cols:
        merged[col] = alt[col].to_numpy()

    base_horizons = sorted(set(pd.to_numeric(merged.get("label_horizon_minutes"), errors="coerce").dropna().astype(int).tolist()))
    base_horizon_ok = (len(base_horizons) == 1 and base_horizons[0] == int(horizon_minutes))

    ce_valid_mask = merged.get("ce_label_valid", pd.Series(index=merged.index, dtype=float)).fillna(0.0) == 1.0
    pe_valid_mask = merged.get("pe_label_valid", pd.Series(index=merged.index, dtype=float)).fillna(0.0) == 1.0
    alt_valid_mask = merged["breakout_label_valid"].fillna(0.0) == 1.0
    candidate_ce_mask = merged["ce_breakout_candidate"].fillna(0.0) == 1.0
    candidate_pe_mask = merged["pe_breakout_candidate"].fillna(0.0) == 1.0

    report: Dict[str, object] = {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T27",
        "status": "completed",
        "config": {
            "horizon_minutes": int(horizon_minutes),
            "return_threshold": float(return_threshold),
        },
        "rows_total": int(len(merged)),
        "days_total": int(merged["trade_date"].astype(str).nunique()) if "trade_date" in merged.columns else 0,
        "time_range": {
            "start": str(merged["timestamp"].iloc[0]) if len(merged) else None,
            "end": str(merged["timestamp"].iloc[-1]) if len(merged) else None,
        },
        "base_label_validation": {
            "base_horizon_minutes_unique": base_horizons,
            "base_horizon_matches_expected": bool(base_horizon_ok),
            "ce_valid_rate": _rate(merged.get("ce_label_valid", pd.Series(dtype=float))),
            "pe_valid_rate": _rate(merged.get("pe_label_valid", pd.Series(dtype=float))),
            "ce_positive_rate": _rate(merged.loc[ce_valid_mask, "ce_label"]) if "ce_label" in merged.columns else None,
            "pe_positive_rate": _rate(merged.loc[pe_valid_mask, "pe_label"]) if "pe_label" in merged.columns else None,
        },
        "breakout_alternative": {
            "alt_valid_rate": _rate(merged["breakout_label_valid"]),
            "ce_candidate_rate": _rate(merged["ce_breakout_candidate"]),
            "pe_candidate_rate": _rate(merged["pe_breakout_candidate"]),
            "ce_positive_rate": _rate(merged.loc[alt_valid_mask, "ce_breakout_label"]),
            "pe_positive_rate": _rate(merged.loc[alt_valid_mask, "pe_breakout_label"]),
            "no_trade_rate": _rate(merged.loc[alt_valid_mask, "breakout_no_trade"]),
            "ce_agreement_with_base_on_candidates": (
                _agreement(merged.get("ce_label", pd.Series(dtype=float)), merged["ce_breakout_label"], ce_valid_mask & alt_valid_mask & candidate_ce_mask)
                if "ce_label" in merged.columns
                else None
            ),
            "pe_agreement_with_base_on_candidates": (
                _agreement(merged.get("pe_label", pd.Series(dtype=float)), merged["pe_breakout_label"], pe_valid_mask & alt_valid_mask & candidate_pe_mask)
                if "pe_label" in merged.columns
                else None
            ),
        },
    }
    return alt, report


def _summary_markdown(report: Dict[str, object]) -> str:
    base = report["base_label_validation"]
    alt = report["breakout_alternative"]
    lines = [
        "# T27 Label/Horizon Validation Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Rows: `{report['rows_total']}` over `{report['days_total']}` days",
        f"- Time range: `{report['time_range']['start']}` -> `{report['time_range']['end']}`",
        "",
        "## Base Label Checks",
        f"- Expected horizon: `{report['config']['horizon_minutes']}`",
        f"- Found horizons: `{base['base_horizon_minutes_unique']}`",
        f"- Horizon match: `{base['base_horizon_matches_expected']}`",
        f"- CE positive rate: `{base['ce_positive_rate']}`",
        f"- PE positive rate: `{base['pe_positive_rate']}`",
        "",
        "## Breakout Alternative",
        f"- Alt valid rate: `{alt['alt_valid_rate']}`",
        f"- CE candidate rate: `{alt['ce_candidate_rate']}`",
        f"- PE candidate rate: `{alt['pe_candidate_rate']}`",
        f"- CE breakout positive rate: `{alt['ce_positive_rate']}`",
        f"- PE breakout positive rate: `{alt['pe_positive_rate']}`",
        f"- No-trade rate: `{alt['no_trade_rate']}`",
    ]
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T27 label and horizon validation (breakout-aware alternative)")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t05_labeled_features.parquet")
    parser.add_argument("--horizon-minutes", type=int, default=3)
    parser.add_argument("--return-threshold", type=float, default=0.002)
    parser.add_argument("--alt-out", default="ml_pipeline/artifacts/t27_breakout_alternative_labels.parquet")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t27_label_validation_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t27_label_validation_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled data not found: {labeled_path}")
        return 2
    labeled = pd.read_parquet(labeled_path)
    alt, report = run_label_horizon_validation(
        labeled_df=labeled,
        horizon_minutes=int(args.horizon_minutes),
        return_threshold=float(args.return_threshold),
    )

    alt_path = Path(args.alt_out)
    report_path = Path(args.report_out)
    summary_path = Path(args.summary_out)
    alt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    alt.to_parquet(alt_path, index=False)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_markdown(report), encoding="utf-8")

    print(f"Rows: {len(labeled)}")
    print(f"Days: {report['days_total']}")
    print(f"Base horizon matches expected: {report['base_label_validation']['base_horizon_matches_expected']}")
    print(f"Alt output: {alt_path}")
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
