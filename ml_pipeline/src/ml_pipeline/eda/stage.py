import argparse
import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..dataset_builder import build_canonical_dataset
from ..canonical_event_builder import (
    apply_option_change_features,
    build_vix_snapshot_for_trade_date,
    extract_option_slice_from_chain,
    chain_from_options_minute,
    safe_float,
)
from ..pipeline_layout import EDA_PROCESSED_ROOT, EDA_RAW_ROOT, ensure_layout_dirs, resolve_market_archive_base, resolve_vix_source
from ..quality_profiler import profile_days
from ..raw_loader import filter_valid_options, load_day_raw_data
from ..schema_validator import discover_available_days
from ..vix_data import load_vix_daily


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _file_inventory(root: Path) -> Dict[str, object]:
    files = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    by_ext: Dict[str, int] = {}
    total_bytes = 0
    for file in files:
        ext = file.suffix.lower() or "<none>"
        by_ext[ext] = by_ext.get(ext, 0) + 1
        total_bytes += int(file.stat().st_size)
    top = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:10]
    return {
        "root": str(root).replace("\\", "/"),
        "files_total": int(len(files)),
        "bytes_total": int(total_bytes),
        "by_extension": dict(sorted(by_ext.items(), key=lambda kv: kv[0])),
        "largest_files": [
            {
                "path": str(p).replace("\\", "/"),
                "bytes": int(p.stat().st_size),
            }
            for p in top
        ],
    }


def _pick_days(*, available_days: Sequence[str], explicit_days: Optional[str], max_days: int) -> List[str]:
    if explicit_days:
        vals = [x.strip() for x in str(explicit_days).split(",") if x.strip()]
        return vals
    if not available_days:
        return []
    tail = list(available_days)[-max(1, int(max_days)) :]
    return [str(d) for d in tail]


def _column_profile(frame: pd.DataFrame) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    n = float(len(frame))
    for col in frame.columns:
        series = frame[col]
        missing = int(series.isna().sum())
        rows.append(
            {
                "column": str(col),
                "dtype": str(series.dtype),
                "missing_count": missing,
                "missing_ratio": (float(missing) / n) if n > 0 else 0.0,
                "unique_count": int(series.nunique(dropna=True)),
            }
        )
    return rows


def _split_by_day(
    frame: pd.DataFrame,
    *,
    train_ratio: float,
    valid_ratio: float,
) -> Dict[str, pd.DataFrame]:
    if frame.empty:
        return {"train": frame.copy(), "valid": frame.copy(), "eval": frame.copy()}
    if not (0.0 < float(train_ratio) < 1.0):
        raise ValueError("train_ratio must be in (0,1)")
    if not (0.0 <= float(valid_ratio) < 1.0):
        raise ValueError("valid_ratio must be in [0,1)")
    if float(train_ratio) + float(valid_ratio) >= 1.0:
        raise ValueError("train_ratio + valid_ratio must be < 1")

    days = sorted({str(d) for d in frame["trade_date"].astype(str).unique()})
    n = len(days)
    train_end = max(1, int(n * float(train_ratio)))
    valid_end = max(train_end + 1, int(n * (float(train_ratio) + float(valid_ratio))))
    valid_end = min(valid_end, n - 1) if n > 2 else min(valid_end, n)

    train_days = set(days[:train_end])
    valid_days = set(days[train_end:valid_end])
    eval_days = set(days[valid_end:])

    out: Dict[str, pd.DataFrame] = {}
    out["train"] = frame[frame["trade_date"].astype(str).isin(train_days)].copy()
    out["valid"] = frame[frame["trade_date"].astype(str).isin(valid_days)].copy()
    out["eval"] = frame[frame["trade_date"].astype(str).isin(eval_days)].copy()
    for key in ("train", "valid", "eval"):
        out[key] = out[key].sort_values("timestamp").reset_index(drop=True)
    return out


def _infer_strike_step(options: pd.DataFrame) -> int:
    if options is None or len(options) == 0 or "strike" not in options.columns:
        return 100
    strikes = sorted({int(x) for x in pd.to_numeric(options["strike"], errors="coerce").dropna().tolist()})
    if len(strikes) < 2:
        return 100
    diffs = [b - a for a, b in zip(strikes[:-1], strikes[1:]) if (b - a) > 0]
    if not diffs:
        return 100
    mode = pd.Series(diffs).mode()
    return int(mode.iloc[0]) if not mode.empty else int(np.median(diffs))


def _filter_options_window(
    options_minute: pd.DataFrame,
    *,
    fut_close: float,
    strike_step: int,
    max_otm_steps: int,
) -> pd.DataFrame:
    if options_minute is None or len(options_minute) == 0:
        return options_minute
    if int(max_otm_steps) <= 0:
        return options_minute
    if not np.isfinite(safe_float(fut_close)):
        return options_minute
    step = max(1, int(strike_step))
    atm = int(round(float(fut_close) / float(step)) * step)
    band = int(max_otm_steps) * step
    lo = atm - band
    hi = atm + band
    out = options_minute[(options_minute["strike"] >= lo) & (options_minute["strike"] <= hi)]
    return out if len(out) else options_minute


def _build_canonical_events_for_day(
    *,
    day: str,
    day_idx: int,
    total_days: int,
    base_path: Path,
    vix_daily: pd.DataFrame,
    max_otm_steps: int,
) -> pd.DataFrame:
    print(f"[eda] canonical-events day {day_idx}/{total_days}: {day}", flush=True)
    raw = load_day_raw_data(base_path=base_path, day=str(day))
    fut = raw.fut.copy().dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    options = filter_valid_options(raw.options.copy())
    options = options.dropna(subset=["timestamp"]).sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    options["strike"] = pd.to_numeric(options["strike"], errors="coerce")
    options = options.dropna(subset=["strike"]).reset_index(drop=True)
    strike_step = _infer_strike_step(options)
    options_by_ts = {ts: grp for ts, grp in options.groupby("timestamp", sort=True)}
    if len(fut) == 0:
        return pd.DataFrame()
    fut_day = fut.loc[:, ["timestamp", "open", "high", "low", "close", "volume", "oi"]].copy()
    for c in ("open", "high", "low", "close", "volume", "oi"):
        fut_day[c] = pd.to_numeric(fut_day[c], errors="coerce")
    close = fut_day["close"].astype(float)
    ret_1m = close.pct_change(1, fill_method=None)
    ret_3m = close.pct_change(3, fill_method=None)
    ret_5m = close.pct_change(5, fill_method=None)
    ema_9 = close.ewm(span=9, adjust=False).mean()
    ema_21 = close.ewm(span=21, adjust=False).mean()
    ema_50 = close.ewm(span=50, adjust=False).mean()
    ema_9_slope = ema_9.diff()
    ema_21_slope = ema_21.diff()
    ema_50_slope = ema_50.diff()
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0)).abs()
    avg_gain = gain.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_14 = 100.0 - (100.0 / (1.0 + rs))
    prev_close = fut_day["close"].shift(1)
    tr = pd.concat(
        [
            (fut_day["high"] - fut_day["low"]).abs(),
            (fut_day["high"] - prev_close).abs(),
            (fut_day["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_14_series = tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    atr_pct = atr_14_series.rank(pct=True)
    typical = (fut_day["high"] + fut_day["low"] + fut_day["close"]) / 3.0
    vol = fut_day["volume"].fillna(0.0)
    fut_vwap = (typical * vol).cumsum() / vol.cumsum().replace(0.0, np.nan)
    day_high_cum = fut_day["high"].cummax()
    day_low_cum = fut_day["low"].cummin()
    opening_n = min(15, len(fut_day))
    opening_high = float(fut_day["high"].head(opening_n).max()) if opening_n > 0 else float("nan")
    opening_low = float(fut_day["low"].head(opening_n).min()) if opening_n > 0 else float("nan")
    day_vix = build_vix_snapshot_for_trade_date(
        vix_daily=vix_daily,
        trade_date=pd.Timestamp(fut_day.iloc[0]["timestamp"]).date(),
    )

    rows: List[Dict[str, object]] = []
    prev_trade_date: Optional[str] = None
    prev_opt0_ce_close: Optional[float] = None
    prev_opt0_pe_close: Optional[float] = None
    prev_opt0_total_oi: Optional[float] = None
    for idx in range(len(fut_day)):
        curr = fut_day.iloc[idx]
        ts = pd.Timestamp(curr["timestamp"])
        fut_close = safe_float(curr.get("close"))
        minute_opts = options_by_ts.get(curr["timestamp"])
        minute_opts = (
            _filter_options_window(
                minute_opts,
                fut_close=fut_close,
                strike_step=strike_step,
                max_otm_steps=int(max_otm_steps),
            )
            if minute_opts is not None
            else pd.DataFrame()
        )
        chain = chain_from_options_minute(minute_opts)
        event: Dict[str, object] = {
            "timestamp": ts.isoformat(),
            "trade_date": str(ts.date()),
            "fut_open": safe_float(curr.get("open")),
            "fut_high": safe_float(curr.get("high")),
            "fut_low": safe_float(curr.get("low")),
            "fut_close": fut_close,
            "fut_volume": safe_float(curr.get("volume")),
            "fut_oi": safe_float(curr.get("oi")),
            "ret_1m": safe_float(ret_1m.iloc[idx]),
            "ret_3m": safe_float(ret_3m.iloc[idx]),
            "ret_5m": safe_float(ret_5m.iloc[idx]),
            "ema_9": safe_float(ema_9.iloc[idx]),
            "ema_21": safe_float(ema_21.iloc[idx]),
            "ema_50": safe_float(ema_50.iloc[idx]),
            "ema_9_21_spread": safe_float(ema_9.iloc[idx] - ema_21.iloc[idx]),
            "ema_9_slope": safe_float(ema_9_slope.iloc[idx]),
            "ema_21_slope": safe_float(ema_21_slope.iloc[idx]),
            "ema_50_slope": safe_float(ema_50_slope.iloc[idx]),
            "rsi_14": safe_float(rsi_14.iloc[idx]),
            "atr_14": safe_float(atr_14_series.iloc[idx]),
            "atr_percentile": safe_float(atr_pct.iloc[idx]),
            "fut_vwap": safe_float(fut_vwap.iloc[idx]),
            "minute_of_day": int(ts.hour * 60 + ts.minute),
            "day_of_week": int(ts.dayofweek),
            "minute_index": int(idx),
            "opening_range_high": opening_high,
            "opening_range_low": opening_low,
            "opening_range_ready": int(idx >= 15),
            "opening_range_breakout_up": int(idx >= 15 and np.isfinite(fut_close) and np.isfinite(opening_high) and fut_close > opening_high),
            "opening_range_breakout_down": int(idx >= 15 and np.isfinite(fut_close) and np.isfinite(opening_low) and fut_close < opening_low),
            "spot_open": float("nan"),
            "spot_high": float("nan"),
            "spot_low": float("nan"),
            "spot_close": float("nan"),
            "basis": float("nan"),
            "basis_change_1m": float("nan"),
            "expiry_code": str(chain.get("expiry", "")).upper().replace("-", ""),
        }
        atr_now = safe_float(event["atr_14"])
        event["atr_ratio"] = safe_float(atr_now / fut_close) if np.isfinite(atr_now) and np.isfinite(fut_close) and fut_close != 0.0 else float("nan")
        vwap_now = safe_float(event["fut_vwap"])
        event["vwap_distance"] = safe_float((fut_close - vwap_now) / vwap_now) if np.isfinite(vwap_now) and vwap_now != 0.0 and np.isfinite(fut_close) else float("nan")
        high_now = safe_float(day_high_cum.iloc[idx])
        low_now = safe_float(day_low_cum.iloc[idx])
        event["distance_from_day_high"] = safe_float((fut_close - high_now) / high_now) if np.isfinite(fut_close) and np.isfinite(high_now) and high_now != 0.0 else float("nan")
        event["distance_from_day_low"] = safe_float((fut_close - low_now) / low_now) if np.isfinite(fut_close) and np.isfinite(low_now) and low_now != 0.0 else float("nan")
        event.update(extract_option_slice_from_chain(chain, fut_price=float(fut_close) if np.isfinite(fut_close) else float("nan")))
        event["ce_pe_oi_diff"] = safe_float(event.get("ce_oi_total")) - safe_float(event.get("pe_oi_total"))
        event["ce_pe_volume_diff"] = safe_float(event.get("ce_volume_total")) - safe_float(event.get("pe_volume_total"))
        event["vix_prev_close"] = safe_float(day_vix.get("vix_prev_close"))
        event["vix_prev_close_change_1d"] = safe_float(day_vix.get("vix_prev_close_change_1d"))
        event["vix_prev_close_zscore_20d"] = safe_float(day_vix.get("vix_prev_close_zscore_20d"))
        event["is_high_vix_day"] = safe_float(day_vix.get("is_high_vix_day"))
        event["atm_call_return_1m"] = float("nan")
        event["atm_put_return_1m"] = float("nan")
        event["atm_oi_change_1m"] = float("nan")
        (
            prev_trade_date,
            prev_opt0_ce_close,
            prev_opt0_pe_close,
            prev_opt0_total_oi,
        ) = apply_option_change_features(
            event,
            prev_trade_date=prev_trade_date,
            prev_opt0_ce_close=prev_opt0_ce_close,
            prev_opt0_pe_close=prev_opt0_pe_close,
            prev_opt0_total_oi=prev_opt0_total_oi,
        )
        event["source_day"] = str(day)
        rows.append(event)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def _build_history_aware_canonical_events(
    *,
    base_path: Path,
    days: Sequence[str],
    vix_daily: pd.DataFrame,
    max_otm_steps: int = 0,
    workers: int = 1,
) -> pd.DataFrame:
    total_days = len(days)
    workers = max(1, int(workers))
    day_frames: List[pd.DataFrame] = []
    if workers == 1:
        for day_idx, day in enumerate(days, start=1):
            frame = _build_canonical_events_for_day(
                day=str(day),
                day_idx=day_idx,
                total_days=total_days,
                base_path=base_path,
                vix_daily=vix_daily,
                max_otm_steps=max_otm_steps,
            )
            if len(frame):
                day_frames.append(frame)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(
                    _build_canonical_events_for_day,
                    day=str(day),
                    day_idx=day_idx,
                    total_days=total_days,
                    base_path=base_path,
                    vix_daily=vix_daily,
                    max_otm_steps=max_otm_steps,
                )
                for day_idx, day in enumerate(days, start=1)
            ]
            for fut in concurrent.futures.as_completed(futures):
                frame = fut.result()
                if len(frame):
                    day_frames.append(frame)

    if not day_frames:
        return pd.DataFrame()
    out = pd.concat(day_frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def run_eda_stage(
    *,
    base_path: Optional[str],
    days: Optional[str],
    max_days: int,
    vix_path: Optional[str],
    out_dir: Optional[Path],
    train_ratio: float,
    valid_ratio: float,
    quality_max_days: int = 90,
    max_otm_steps: int = 0,
    workers: int = 1,
) -> Dict[str, object]:
    ensure_layout_dirs()
    processed_root = Path(out_dir) if out_dir is not None else EDA_PROCESSED_ROOT
    processed_root.mkdir(parents=True, exist_ok=True)

    archive = resolve_market_archive_base(explicit_base=base_path)
    available_days = discover_available_days(archive) if archive is not None else []
    selected_days = _pick_days(available_days=available_days, explicit_days=days, max_days=max_days)
    vix_source = resolve_vix_source(explicit_vix=vix_path)
    vix_daily = load_vix_daily(vix_source) if vix_source else pd.DataFrame()

    print(
        f"[eda] archive={str(archive) if archive else None} selected_days={len(selected_days)} "
        f"vix_source={str(vix_source) if vix_source else None}",
        flush=True,
    )
    inventory = {
        "created_at_utc": _utc_now(),
        "archive_base_resolved": (str(archive).replace("\\", "/") if archive else None),
        "available_days_total": int(len(available_days)),
        "selected_days": selected_days,
        "selected_days_total": int(len(selected_days)),
        "vix_source_resolved": (str(vix_source).replace("\\", "/") if vix_source else None),
        "vix_rows": int(len(vix_daily)),
        "raw_data_inventory": _file_inventory(EDA_RAW_ROOT),
    }
    _write_json(processed_root / "eda_inventory.json", inventory)

    quality_report: Dict[str, object] = {
        "created_at_utc": _utc_now(),
        "status": "skipped",
        "reason": "no selected days",
    }
    canonical_path = processed_root / "canonical_panel_sample.parquet"
    canonical_events_path = processed_root / "canonical_events.parquet"
    schema_path = processed_root / "canonical_schema.json"
    splits_dir = processed_root / "datasets"
    split_report_path = processed_root / "dataset_split_report.json"
    if archive is not None and selected_days:
        print("[eda] profiling quality...", flush=True)
        quality_days = selected_days[-min(len(selected_days), max(1, int(quality_max_days))):]
        quality_report = profile_days(base_path=archive, days=quality_days)
        quality_report["created_at_utc"] = _utc_now()
        quality_report["stage"] = "eda_quality"
        quality_report["selected_days_total"] = int(len(selected_days))
        quality_report["quality_profile_days_total"] = int(len(quality_days))
        _write_json(processed_root / "eda_quality_report.json", quality_report)

        # Keep this as a true sample to avoid a second full multi-year pass.
        sample_days = selected_days[-min(len(selected_days), 5) :]
        print(f"[eda] building canonical panel sample (days={len(sample_days)})...", flush=True)
        canonical = build_canonical_dataset(base_path=archive, days=sample_days)
        canonical.to_parquet(canonical_path, index=False)

        print("[eda] building history-aware canonical events...", flush=True)
        canonical_events = _build_history_aware_canonical_events(
            base_path=archive,
            days=selected_days,
            vix_daily=vix_daily,
            max_otm_steps=int(max_otm_steps),
            workers=int(workers),
        )
        canonical_events.to_parquet(canonical_events_path, index=False)
        print(f"[eda] canonical events rows={len(canonical_events)}", flush=True)

        schema = {
            "created_at_utc": _utc_now(),
            "rows": int(len(canonical_events)),
            "columns": [str(c) for c in canonical_events.columns],
            "column_profile": _column_profile(canonical_events),
            "keys": ["timestamp", "trade_date"],
            "notes": "History-aware canonical events (shared builder parity with live) for downstream feature engineering.",
        }
        _write_json(schema_path, schema)

        print("[eda] splitting datasets...", flush=True)
        splits = _split_by_day(canonical_events, train_ratio=float(train_ratio), valid_ratio=float(valid_ratio))
        splits_dir.mkdir(parents=True, exist_ok=True)
        split_report = {
            "created_at_utc": _utc_now(),
            "method": "time_ordered_day_split",
            "ratios": {"train_ratio": float(train_ratio), "valid_ratio": float(valid_ratio), "eval_ratio": 1.0 - float(train_ratio) - float(valid_ratio)},
            "rows": {k: int(len(v)) for k, v in splits.items()},
            "days": {
                k: sorted({str(x) for x in v["trade_date"].astype(str).unique().tolist()}) if len(v) else []
                for k, v in splits.items()
            },
            "outputs": {},
        }
        for key in ("train", "valid", "eval"):
            dst = splits_dir / f"{key}.parquet"
            splits[key].to_parquet(dst, index=False)
            split_report["outputs"][f"{key}_parquet"] = str(dst).replace("\\", "/")
        _write_json(split_report_path, split_report)
    else:
        _write_json(processed_root / "eda_quality_report.json", quality_report)
        _write_json(
            schema_path,
            {
                "created_at_utc": _utc_now(),
                "rows": 0,
                "columns": [],
                "column_profile": [],
                "keys": ["timestamp", "trade_date"],
                "notes": "No canonical events generated yet (missing selected days/archive input).",
            },
        )
        _write_json(
            split_report_path,
            {
                "created_at_utc": _utc_now(),
                "method": "time_ordered_day_split",
                "status": "skipped",
                "reason": "no canonical dataset generated",
                "outputs": {},
            },
        )

    summary = {
        "created_at_utc": _utc_now(),
        "processed_root": str(processed_root).replace("\\", "/"),
        "outputs": {
            "eda_inventory_json": str((processed_root / "eda_inventory.json")).replace("\\", "/"),
            "eda_quality_report_json": str((processed_root / "eda_quality_report.json")).replace("\\", "/"),
            "canonical_panel_sample_parquet": str(canonical_path).replace("\\", "/"),
            "canonical_events_parquet": str(canonical_events_path).replace("\\", "/"),
            "canonical_schema_json": str(schema_path).replace("\\", "/"),
            "dataset_split_report_json": str(split_report_path).replace("\\", "/"),
            "datasets_dir": str(splits_dir).replace("\\", "/"),
        },
        "selected_days_total": int(len(selected_days)),
        "max_otm_steps": int(max_otm_steps),
        "workers": int(workers),
        "archive_base_resolved": (str(archive).replace("\\", "/") if archive else None),
        "vix_source_resolved": (str(vix_source).replace("\\", "/") if vix_source else None),
    }
    _write_json(processed_root / "eda_run_summary.json", summary)
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EDA stage: inventory + quality + canonical processed sample")
    parser.add_argument("--base-path", default=None, help="Optional local historical archive base")
    parser.add_argument("--days", default=None, help="Optional explicit day list: YYYY-MM-DD,YYYY-MM-DD")
    parser.add_argument("--max-days", type=int, default=5, help="If --days is not set, use latest N available days")
    parser.add_argument("--vix-path", default=None, help="Optional explicit VIX file/dir")
    parser.add_argument("--out-dir", default=None, help="Optional processed output dir")
    parser.add_argument("--train-ratio", type=float, default=0.70, help="Day-level train ratio")
    parser.add_argument("--valid-ratio", type=float, default=0.15, help="Day-level validation ratio")
    parser.add_argument(
        "--quality-max-days",
        type=int,
        default=90,
        help="Profile quality on latest N selected days (speed guard).",
    )
    parser.add_argument(
        "--max-otm-steps",
        type=int,
        default=0,
        help="Option-chain strike window around ATM in strike-steps (0 disables filtering).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel day workers for canonical event build.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    out = run_eda_stage(
        base_path=args.base_path,
        days=args.days,
        max_days=int(args.max_days),
        vix_path=args.vix_path,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        train_ratio=float(args.train_ratio),
        valid_ratio=float(args.valid_ratio),
        quality_max_days=int(args.quality_max_days),
        max_otm_steps=int(args.max_otm_steps),
        workers=int(args.workers),
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
