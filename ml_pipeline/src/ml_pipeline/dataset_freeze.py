import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .train_baseline import (
    FEATURE_PROFILE_ALL,
    FEATURE_PROFILES,
    select_feature_columns,
)
from .live_inference_adapter import LiveMarketFeatureClient, _build_feature_row_from_ohlc_and_chain


IST = timezone(timedelta(hours=5, minutes=30))


def _ensure_sorted_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if "trade_date" not in out.columns:
        out["trade_date"] = out["timestamp"].dt.date.astype(str)
    else:
        out["trade_date"] = out["trade_date"].astype(str)
    return out


def build_day_split(days: Sequence[str], train_ratio: float = 0.70, valid_ratio: float = 0.15) -> Dict[str, List[str]]:
    unique_days = sorted({str(x) for x in days})
    n = len(unique_days)
    if n < 3:
        raise ValueError("need at least 3 unique trade days for train/valid/test split")
    if not (0.0 < float(train_ratio) < 1.0):
        raise ValueError("train_ratio must be in (0,1)")
    if not (0.0 < float(valid_ratio) < 1.0):
        raise ValueError("valid_ratio must be in (0,1)")
    if (float(train_ratio) + float(valid_ratio)) >= 1.0:
        raise ValueError("train_ratio + valid_ratio must be < 1")

    train_n = max(1, int(np.floor(n * float(train_ratio))))
    valid_n = max(1, int(np.floor(n * float(valid_ratio))))
    if train_n + valid_n >= n:
        valid_n = max(1, n - train_n - 1)
    if train_n + valid_n >= n:
        train_n = max(1, n - valid_n - 1)
    test_n = n - train_n - valid_n
    if test_n <= 0:
        raise ValueError("split failed to allocate test partition")

    train_days = unique_days[:train_n]
    valid_days = unique_days[train_n : train_n + valid_n]
    test_days = unique_days[train_n + valid_n :]
    return {
        "train_days": train_days,
        "valid_days": valid_days,
        "test_days": test_days,
    }


def _rows_for_days(df: pd.DataFrame, day_list: Sequence[str]) -> pd.DataFrame:
    target = {str(x) for x in day_list}
    out = df[df["trade_date"].astype(str).isin(target)].copy()
    return out.sort_values("timestamp").reset_index(drop=True)


def _split_summary(df: pd.DataFrame, feature_columns: Sequence[str]) -> Dict[str, object]:
    ce_valid = df["ce_label_valid"] == 1.0 if "ce_label_valid" in df.columns else pd.Series(dtype=bool)
    pe_valid = df["pe_label_valid"] == 1.0 if "pe_label_valid" in df.columns else pd.Series(dtype=bool)
    ce_positive_rate = None
    pe_positive_rate = None
    if len(df) > 0 and "ce_label" in df.columns and "ce_label_valid" in df.columns:
        ce_series = df.loc[ce_valid, "ce_label"].astype(float)
        ce_positive_rate = float(ce_series.mean()) if len(ce_series) > 0 else None
    if len(df) > 0 and "pe_label" in df.columns and "pe_label_valid" in df.columns:
        pe_series = df.loc[pe_valid, "pe_label"].astype(float)
        pe_positive_rate = float(pe_series.mean()) if len(pe_series) > 0 else None

    all_nan_features = []
    high_missing_features = []
    for col in feature_columns:
        if col not in df.columns:
            continue
        miss_rate = float(df[col].isna().mean()) if len(df) else 0.0
        if miss_rate >= 0.999999:
            all_nan_features.append(col)
        if miss_rate >= 0.50:
            high_missing_features.append({"feature": col, "missing_rate": miss_rate})
    high_missing_features = sorted(high_missing_features, key=lambda x: x["missing_rate"], reverse=True)

    return {
        "rows": int(len(df)),
        "days": int(df["trade_date"].nunique()) if "trade_date" in df.columns else 0,
        "start_timestamp": str(df["timestamp"].iloc[0]) if len(df) else None,
        "end_timestamp": str(df["timestamp"].iloc[-1]) if len(df) else None,
        "ce_positive_rate": ce_positive_rate,
        "pe_positive_rate": pe_positive_rate,
        "all_nan_feature_count": int(len(all_nan_features)),
        "all_nan_features": all_nan_features,
        "high_missing_features_top10": high_missing_features[:10],
    }


def _sha256(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def build_synthetic_live_feature_row() -> Dict[str, object]:
    ts = pd.date_range("2023-06-15 09:15:00", periods=90, freq="min")
    base = 44200.0 + np.linspace(-40.0, 55.0, len(ts))
    close = base + (np.sin(np.linspace(0.0, 6.0, len(ts))) * 12.0)
    open_px = np.roll(close, 1)
    open_px[0] = close[0]
    high = np.maximum(open_px, close) + 6.0
    low = np.minimum(open_px, close) - 6.0
    volume = 1200.0 + np.arange(len(ts)) * 8.0
    oi = 50000.0 + np.arange(len(ts)) * 21.0

    ohlc = pd.DataFrame(
        {
            "timestamp": ts,
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "oi": oi,
        }
    )
    fut_price = float(close[-1])
    strike_step = 100.0
    atm_strike = float(round(fut_price / strike_step) * strike_step)
    strike_ladder = [atm_strike + (k * strike_step) for k in (-2, -1, 0, 1, 2)]

    strikes: List[Dict[str, float]] = []
    for strike in strike_ladder:
        distance = abs(fut_price - strike) / 100.0
        premium_base = 12.0 + (8.0 / (1.0 + distance))
        ce_intrinsic = max(0.0, fut_price - strike)
        pe_intrinsic = max(0.0, strike - fut_price)
        strikes.append(
            {
                "strike": strike,
                "ce_ltp": float(premium_base + ce_intrinsic * 0.06),
                "pe_ltp": float(premium_base + pe_intrinsic * 0.06),
                "ce_oi": float(21000.0 - distance * 400.0),
                "pe_oi": float(20500.0 + distance * 450.0),
                "ce_volume": float(1100.0 + (2.0 - distance) * 140.0),
                "pe_volume": float(1080.0 + (2.0 - distance) * 120.0),
            }
        )
    chain = {
        "expiry": "2023-06-29",
        "pcr": 1.05,
        "strikes": strikes,
    }
    row = _build_feature_row_from_ohlc_and_chain(
        ohlc=ohlc,
        chain=chain,
        options_extractor=LiveMarketFeatureClient._extract_option_slice,
        rsi_fn=LiveMarketFeatureClient._rsi,
        atr_fn=LiveMarketFeatureClient._atr,
        vwap_fn=LiveMarketFeatureClient._vwap,
    )
    return row


def evaluate_dataset_freeze(
    labeled_df: pd.DataFrame,
    feature_profile: str = FEATURE_PROFILE_ALL,
    train_ratio: float = 0.70,
    valid_ratio: float = 0.15,
    input_path: Optional[Path] = None,
) -> Dict[str, object]:
    frame = _ensure_sorted_frame(labeled_df)
    day_split = build_day_split(frame["trade_date"].tolist(), train_ratio=train_ratio, valid_ratio=valid_ratio)
    feature_columns = select_feature_columns(frame, feature_profile=feature_profile)
    if not feature_columns:
        raise ValueError("no feature columns selected for freeze")

    train_df = _rows_for_days(frame, day_split["train_days"])
    valid_df = _rows_for_days(frame, day_split["valid_days"])
    test_df = _rows_for_days(frame, day_split["test_days"])
    split_frames = {"train": train_df, "valid": valid_df, "test": test_df}

    split_missing_columns: Dict[str, List[str]] = {}
    for split_name, split_df in split_frames.items():
        split_missing_columns[split_name] = [c for c in feature_columns if c not in split_df.columns]
    train_eval_parity_ok = all(len(v) == 0 for v in split_missing_columns.values())

    live_row = build_synthetic_live_feature_row()
    live_columns = sorted(live_row.keys())
    missing_in_live = sorted([c for c in feature_columns if c not in live_row])
    live_parity_ok = len(missing_in_live) == 0

    input_meta: Dict[str, object] = {
        "path": str(input_path) if input_path is not None else None,
        "sha256": _sha256(input_path) if input_path is not None else None,
        "size_bytes": int(input_path.stat().st_size) if input_path is not None and input_path.exists() else None,
    }

    report = {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T26",
        "status": "completed",
        "feature_profile": str(feature_profile),
        "split_config": {
            "train_ratio": float(train_ratio),
            "valid_ratio": float(valid_ratio),
            "test_ratio": float(1.0 - float(train_ratio) - float(valid_ratio)),
            "split_unit": "trade_date",
        },
        "input_dataset": input_meta,
        "dataset_summary": {
            "rows_total": int(len(frame)),
            "days_total": int(frame["trade_date"].nunique()),
            "start_timestamp": str(frame["timestamp"].iloc[0]) if len(frame) else None,
            "end_timestamp": str(frame["timestamp"].iloc[-1]) if len(frame) else None,
            "column_count": int(len(frame.columns)),
        },
        "feature_set": {
            "feature_count": int(len(feature_columns)),
            "feature_columns": list(feature_columns),
        },
        "day_split": day_split,
        "split_summaries": {
            "train": _split_summary(train_df, feature_columns),
            "valid": _split_summary(valid_df, feature_columns),
            "test": _split_summary(test_df, feature_columns),
        },
        "parity": {
            "train_eval": {
                "parity_ok": bool(train_eval_parity_ok),
                "missing_columns_by_split": split_missing_columns,
            },
            "live": {
                "parity_ok": bool(live_parity_ok),
                "missing_in_live_contract": missing_in_live,
                "live_contract_column_count": int(len(live_columns)),
            },
        },
    }
    return report


def _summary_markdown(report: Dict[str, object]) -> str:
    ds = report["dataset_summary"]
    split = report["split_summaries"]
    parity = report["parity"]
    lines = [
        "# T26 Dataset Freeze Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Feature profile: `{report['feature_profile']}`",
        f"- Rows: `{ds['rows_total']}` across `{ds['days_total']}` trade days",
        f"- Time range: `{ds['start_timestamp']}` -> `{ds['end_timestamp']}`",
        "",
        "## Split",
        f"- Train days: `{len(report['day_split']['train_days'])}`",
        f"- Valid days: `{len(report['day_split']['valid_days'])}`",
        f"- Test days: `{len(report['day_split']['test_days'])}`",
        "",
        "## Rows by split",
        f"- Train: `{split['train']['rows']}` rows",
        f"- Valid: `{split['valid']['rows']}` rows",
        f"- Test: `{split['test']['rows']}` rows",
        "",
        "## Parity Checks",
        f"- Train/Eval parity: `{parity['train_eval']['parity_ok']}`",
        f"- Live parity (synthetic contract): `{parity['live']['parity_ok']}`",
        f"- Missing in live contract: `{len(parity['live']['missing_in_live_contract'])}`",
    ]
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T26 futures+options-only dataset freeze + parity report")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled feature parquet",
    )
    parser.add_argument(
        "--feature-profile",
        default="futures_options_only",
        choices=list(FEATURE_PROFILES),
        help="Feature profile for freeze",
    )
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--valid-ratio", type=float, default=0.15)
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t26_dataset_freeze_report.json",
        help="Output JSON report",
    )
    parser.add_argument(
        "--summary-out",
        default="ml_pipeline/artifacts/t26_dataset_freeze_summary.md",
        help="Output markdown summary",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2

    df = pd.read_parquet(labeled_path)
    report = evaluate_dataset_freeze(
        labeled_df=df,
        feature_profile=str(args.feature_profile),
        train_ratio=float(args.train_ratio),
        valid_ratio=float(args.valid_ratio),
        input_path=labeled_path,
    )

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)

    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_out.write_text(_summary_markdown(report), encoding="utf-8")

    print(f"Input rows: {len(df)}")
    print(f"Feature profile: {report['feature_profile']}")
    print(f"Features selected: {report['feature_set']['feature_count']}")
    print(f"Train/Eval parity: {report['parity']['train_eval']['parity_ok']}")
    print(f"Live parity: {report['parity']['live']['parity_ok']}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
