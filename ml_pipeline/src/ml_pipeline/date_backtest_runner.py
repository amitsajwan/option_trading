import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .dataset_builder import build_canonical_day_panel
from .feature.engineering import build_feature_table
from .fill_model import FillModelConfig
from .label_engine import _effective_config, label_day
from .live_inference_adapter import (
    DecisionThresholds,
    load_model_package,
    load_thresholds,
    run_replay_dry_run_v2,
)
from .paper_replay_evaluation import (
    _profile_from_t19,
    evaluate_replay,
    load_decisions_jsonl,
)
from .pipeline_layout import ensure_layout_dirs, resolve_market_archive_base, resolve_vix_source
from .raw_loader import DayRawData, load_day_raw_data
from .schema_validator import build_file_path
from .vix_auto_fetch import ensure_vix_history_for_trade_day

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover - optional dependency at runtime
    MongoClient = None


IST = timezone(timedelta(hours=5, minutes=30))


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _sanitize_token(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(text or "").strip())


def _ensure_ist_day(day: str) -> str:
    return datetime.strptime(str(day), "%Y-%m-%d").strftime("%Y-%m-%d")


def _day_window_utc(day: str) -> Tuple[datetime, datetime]:
    start_ist = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def _to_ist_minute(value: Any) -> Optional[pd.Timestamp]:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts.tz_convert(IST).floor("min").tz_localize(None)


def _to_ist_trade_day(value: Any) -> Optional[str]:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return str(ts.tz_convert(IST).date())


def _local_day_available(base_path: Path, day: str) -> bool:
    for ds in ("fut", "options", "spot"):
        p = build_file_path(base_path=base_path, dataset=ds, day=day)
        if not p.exists():
            return False
    return True


def _load_local_day(
    *,
    base_path: Path,
    day: str,
    vix_path: Optional[str],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    raw = load_day_raw_data(base_path=base_path, day=day)
    panel = build_canonical_day_panel(raw)
    features = build_feature_table(panel, vix_source=vix_path)
    cfg = _effective_config(
        horizon_minutes=None,
        return_threshold=None,
        use_excursion_gate=None,
        min_favorable_excursion=None,
        max_adverse_excursion=None,
        stop_loss_pct=None,
        take_profit_pct=None,
        allow_hold_extension=None,
        extension_trigger_profit_pct=None,
    )
    labeled = label_day(features, raw.options.copy(), cfg)
    meta = {
        "source": "local_archive",
        "base_path": str(base_path),
        "raw_fut_rows": int(len(raw.fut)),
        "raw_options_rows": int(len(raw.options)),
        "raw_spot_rows": int(len(raw.spot)),
    }
    return panel, features, labeled, meta


def _mongo_find_rows(
    *,
    coll,
    instrument: str,
    start_utc: datetime,
    end_utc: datetime,
) -> List[Dict[str, Any]]:
    # Mongo often stores UTC datetimes without tzinfo; query in both forms.
    start_naive = start_utc.replace(tzinfo=None)
    end_naive = end_utc.replace(tzinfo=None)
    query = {
        "instrument": instrument,
        "$or": [
            {"timestamp": {"$gte": start_naive, "$lt": end_naive}},
            {"timestamp": {"$gte": start_utc, "$lt": end_utc}},
            {"market_minute": {"$gte": start_naive, "$lt": end_naive}},
            {"market_minute": {"$gte": start_utc, "$lt": end_utc}},
        ],
    }
    rows = list(coll.find(query))
    if rows:
        return rows
    # Fallback: read recent instrument rows and filter in python by IST day.
    recent = list(coll.find({"instrument": instrument}).sort("timestamp", -1).limit(250000))
    out: List[Dict[str, Any]] = []
    for item in recent:
        ts = item.get("timestamp") or item.get("market_minute")
        trade_day = _to_ist_trade_day(ts)
        if trade_day == str(start_utc.astimezone(IST).date()):
            out.append(item)
    return out


def _parse_expiry_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    # 20260330 -> 30MAR26
    if len(text) == 8 and text.isdigit():
        try:
            dt = datetime.strptime(text, "%Y%m%d")
            return dt.strftime("%d%b%y").upper()
        except Exception:
            return ""
    # 2026-03-30 -> 30MAR26
    if "-" in text:
        try:
            dt = datetime.fromisoformat(text)
            return dt.strftime("%d%b%y").upper()
        except Exception:
            return ""
    # Already in 30MAR26 style
    if len(text) == 7 and text[:2].isdigit() and text[-2:].isdigit():
        return text
    return ""


def _build_depth_frame(depth_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    out_rows: List[Dict[str, Any]] = []
    for row in depth_rows:
        minute = _to_ist_minute(row.get("timestamp") or row.get("market_minute"))
        if minute is None:
            continue
        buy = row.get("buy_depth") or []
        sell = row.get("sell_depth") or []
        top_bid = buy[0] if isinstance(buy, list) and len(buy) > 0 and isinstance(buy[0], dict) else {}
        top_ask = sell[0] if isinstance(sell, list) and len(sell) > 0 and isinstance(sell[0], dict) else {}
        total_bid = _safe_float(row.get("total_bid_qty"))
        total_ask = _safe_float(row.get("total_ask_qty"))
        top_bid_px = _safe_float(top_bid.get("price"))
        top_ask_px = _safe_float(top_ask.get("price"))
        spread = top_ask_px - top_bid_px if pd.notna(top_ask_px) and pd.notna(top_bid_px) else float("nan")
        denom = total_bid + total_ask
        imbalance = ((total_bid - total_ask) / denom) if pd.notna(denom) and denom > 0 else float("nan")
        out_rows.append(
            {
                "timestamp": minute,
                "trade_date": str(minute.date()),
                "depth_total_bid_qty": total_bid,
                "depth_total_ask_qty": total_ask,
                "depth_top_bid_qty": _safe_float(top_bid.get("quantity")),
                "depth_top_ask_qty": _safe_float(top_ask.get("quantity")),
                "depth_top_bid_price": top_bid_px,
                "depth_top_ask_price": top_ask_px,
                "depth_spread": spread,
                "depth_imbalance": imbalance,
            }
        )
    if not out_rows:
        return pd.DataFrame()
    frame = pd.DataFrame(out_rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates(
        subset=["timestamp"], keep="last"
    )
    return frame.reset_index(drop=True)


def _build_fut_frame(tick_rows: List[Dict[str, Any]], instrument: str) -> pd.DataFrame:
    out: List[Dict[str, Any]] = []
    for row in tick_rows:
        minute = _to_ist_minute(row.get("timestamp") or row.get("market_minute"))
        if minute is None:
            continue
        price = _safe_float(row.get("last_price"))
        if not pd.notna(price):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            price = _safe_float(payload.get("last_price"))
        if not pd.notna(price):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        out.append(
            {
                "minute": minute,
                "price": price,
                "oi": _safe_float(row.get("oi") if row.get("oi") is not None else payload.get("oi")),
                "cum_vol": _safe_float(
                    row.get("cumulative_volume")
                    if row.get("cumulative_volume") is not None
                    else payload.get("cumulative_volume")
                ),
                "candle_volume": _safe_float(
                    row.get("candle_volume")
                    if row.get("candle_volume") is not None
                    else payload.get("candle_volume")
                ),
            }
        )
    if not out:
        return pd.DataFrame()
    ticks = pd.DataFrame(out).sort_values("minute").reset_index(drop=True)
    grouped = ticks.groupby("minute", as_index=False).agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        oi=("oi", "last"),
        cum_vol_last=("cum_vol", "last"),
        candle_volume_sum=("candle_volume", "sum"),
    )
    grouped["volume"] = grouped["cum_vol_last"].diff().clip(lower=0.0)
    use_fallback = grouped["volume"].isna() | (grouped["volume"] <= 0)
    grouped.loc[use_fallback, "volume"] = grouped.loc[use_fallback, "candle_volume_sum"].clip(lower=0.0)
    grouped["symbol"] = instrument
    grouped["timestamp"] = pd.to_datetime(grouped["minute"], errors="coerce")
    grouped["trade_date"] = grouped["timestamp"].dt.date.astype(str)
    return grouped.loc[:, ["timestamp", "trade_date", "symbol", "open", "high", "low", "close", "oi", "volume"]]


def _build_options_frame(options_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    recs: List[Dict[str, Any]] = []
    for row in options_rows:
        snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
        minute = _to_ist_minute(row.get("timestamp") or snapshot.get("timestamp") or row.get("market_minute"))
        if minute is None:
            continue
        expiry_code = _parse_expiry_code(snapshot.get("expiry") or row.get("expiry"))
        strikes = snapshot.get("strikes")
        if not isinstance(strikes, list):
            continue
        for strike_row in strikes:
            if not isinstance(strike_row, dict):
                continue
            strike = _safe_float(strike_row.get("strike"))
            if not pd.notna(strike):
                continue
            strike_i = int(round(float(strike)))
            for side, ltp_key, oi_key, vol_key in (
                ("CE", "ce_ltp", "ce_oi", "ce_volume"),
                ("PE", "pe_ltp", "pe_oi", "pe_volume"),
            ):
                ltp = _safe_float(strike_row.get(ltp_key))
                if not pd.notna(ltp):
                    continue
                symbol = f"BANKNIFTY{expiry_code}{strike_i}{side}" if expiry_code else ""
                recs.append(
                    {
                        "timestamp": minute,
                        "trade_date": str(minute.date()),
                        "symbol": symbol,
                        "open": ltp,
                        "high": ltp,
                        "low": ltp,
                        "close": ltp,
                        "oi": _safe_float(strike_row.get(oi_key)),
                        "volume": _safe_float(strike_row.get(vol_key)),
                        "expiry_code": expiry_code,
                        "strike": strike_i,
                        "option_type": side,
                    }
                )
    if not recs:
        return pd.DataFrame()
    frame = pd.DataFrame(recs).sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    # If multiple snapshots per minute exist, build minute OHLC from those snapshots.
    agg = frame.groupby(["timestamp", "symbol"], as_index=False).agg(
        trade_date=("trade_date", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        oi=("oi", "last"),
        volume=("volume", "last"),
        expiry_code=("expiry_code", "last"),
        strike=("strike", "last"),
        option_type=("option_type", "last"),
    )
    # Prefer dominant expiry for the day.
    valid = agg[agg["expiry_code"].astype(str).str.len() > 0]
    if not valid.empty:
        dominant_expiry = str(valid["expiry_code"].value_counts().idxmax())
        agg = agg[(agg["expiry_code"] == dominant_expiry) | (agg["expiry_code"].astype(str).str.len() == 0)].copy()
    return agg.reset_index(drop=True)


def _load_mongo_day(
    *,
    day: str,
    instrument: str,
    mongo_uri: str,
    mongo_db: str,
    vix_path: Optional[str],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    if MongoClient is None:
        raise RuntimeError("pymongo is not available in this environment")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    db = client[mongo_db]
    start_utc, end_utc = _day_window_utc(day)

    tick_rows = _mongo_find_rows(
        coll=db["live_ticks"],
        instrument=instrument,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    options_rows = _mongo_find_rows(
        coll=db["live_options_chain"],
        instrument=instrument,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    depth_rows = _mongo_find_rows(
        coll=db["live_depth"],
        instrument=instrument,
        start_utc=start_utc,
        end_utc=end_utc,
    )

    fut = _build_fut_frame(tick_rows=tick_rows, instrument=instrument)
    if fut.empty:
        raise RuntimeError(f"no futures ticks in mongo for {instrument} on {day}")
    options = _build_options_frame(options_rows)
    if options.empty:
        raise RuntimeError(f"no options snapshots in mongo for {instrument} on {day}")
    spot = fut.loc[:, ["timestamp", "trade_date", "symbol", "open", "high", "low", "close"]].copy()
    spot["symbol"] = "BANKNIFTY"
    raw = DayRawData(day=day, fut=fut.copy(), options=options.copy(), spot=spot.copy())
    depth_frame = _build_depth_frame(depth_rows)
    panel = build_canonical_day_panel(raw, depth_frame=depth_frame)
    features = build_feature_table(panel, vix_source=vix_path)
    cfg = _effective_config(
        horizon_minutes=None,
        return_threshold=None,
        use_excursion_gate=None,
        min_favorable_excursion=None,
        max_adverse_excursion=None,
        stop_loss_pct=None,
        take_profit_pct=None,
        allow_hold_extension=None,
        extension_trigger_profit_pct=None,
    )
    labeled = label_day(features, options.copy(), cfg)
    meta = {
        "source": "mongo_live_payloads",
        "mongo_db": mongo_db,
        "mongo_uri": mongo_uri,
        "raw_tick_docs": int(len(tick_rows)),
        "raw_options_docs": int(len(options_rows)),
        "raw_depth_docs": int(len(depth_rows)),
        "reconstructed_fut_rows": int(len(fut)),
        "reconstructed_options_rows": int(len(options)),
        "reconstructed_depth_rows": int(len(depth_frame)),
    }
    return panel, features, labeled, meta


def run_date_backtest(
    *,
    day: str,
    instrument: str,
    model_package_path: Path,
    threshold_report_path: Path,
    out_dir: Path,
    source: str = "auto",
    base_path: Optional[str] = None,
    mongo_uri: Optional[str] = None,
    mongo_db: Optional[str] = None,
    vix_path: Optional[str] = None,
    t19_report_path: Optional[Path] = None,
    tag: Optional[str] = None,
    ce_threshold_override: Optional[float] = None,
    pe_threshold_override: Optional[float] = None,
) -> Dict[str, Any]:
    day = _ensure_ist_day(day)
    instrument = str(instrument or "").strip().upper()
    if not instrument:
        raise ValueError("instrument is required")
    if not model_package_path.exists():
        raise FileNotFoundError(f"model package not found: {model_package_path}")
    if not threshold_report_path.exists():
        raise FileNotFoundError(f"threshold report not found: {threshold_report_path}")

    resolved_source = str(source or "auto").strip().lower()
    if resolved_source not in {"auto", "local", "mongo"}:
        raise ValueError("source must be one of: auto, local, mongo")

    ensure_layout_dirs()
    try:
        ensure_vix_history_for_trade_day(trade_day=day)
    except Exception:
        # Keep backtest path resilient: strict input gate will surface missing VIX downstream.
        pass
    local_base = resolve_market_archive_base(explicit_base=base_path)
    vix_source = resolve_vix_source(explicit_vix=vix_path)
    local_available = bool(local_base and _local_day_available(local_base, day))

    if resolved_source == "local" and not local_available:
        raise RuntimeError(f"local archive data not available for {day}")
    if resolved_source == "auto":
        resolved_source = "local" if local_available else "mongo"

    if resolved_source == "mongo":
        mongo_uri_final = str(mongo_uri or os.getenv("MONGODB_URI") or "mongodb://localhost:27017/")
        mongo_db_final = str(mongo_db or os.getenv("MONGO_DB") or "trading_ai")
        panel, features, labeled, source_meta = _load_mongo_day(
            day=day,
            instrument=instrument,
            mongo_uri=mongo_uri_final,
            mongo_db=mongo_db_final,
            vix_path=vix_source,
        )
    else:
        if local_base is None:
            raise RuntimeError("local archive base path is not configured")
        panel, features, labeled, source_meta = _load_local_day(
            base_path=local_base,
            day=day,
            vix_path=vix_source,
        )

    if features.empty:
        raise RuntimeError("feature table is empty")
    if labeled.empty:
        raise RuntimeError("labeled dataset is empty")

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_tag = _sanitize_token(tag or f"{day}_{instrument}_{resolved_source}_{ts_tag}")
    run_dir = out_dir / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)

    panel_path = run_dir / "panel.parquet"
    features_path = run_dir / "features.parquet"
    labeled_path = run_dir / "labeled.parquet"
    decisions_path = run_dir / "decisions.jsonl"
    trades_path = run_dir / "replay_trades.parquet"
    report_path = run_dir / "replay_report.json"
    full_report_path = run_dir / "full_report.json"

    panel.to_parquet(panel_path, index=False)
    features.to_parquet(features_path, index=False)
    labeled.to_parquet(labeled_path, index=False)

    model_package = load_model_package(model_package_path)
    loaded_thresholds = load_thresholds(threshold_report_path)
    ce_threshold = float(loaded_thresholds.ce)
    pe_threshold = float(loaded_thresholds.pe)
    if ce_threshold_override is not None:
        ce_candidate = float(ce_threshold_override)
        if ce_candidate < 0.0 or ce_candidate > 1.0:
            raise ValueError(f"ce_threshold_override must be within [0, 1], got {ce_candidate}")
        ce_threshold = ce_candidate
    if pe_threshold_override is not None:
        pe_candidate = float(pe_threshold_override)
        if pe_candidate < 0.0 or pe_candidate > 1.0:
            raise ValueError(f"pe_threshold_override must be within [0, 1], got {pe_candidate}")
        pe_threshold = pe_candidate
    thresholds = DecisionThresholds(
        ce=ce_threshold,
        pe=pe_threshold,
        cost_per_trade=float(loaded_thresholds.cost_per_trade),
    )
    replay_summary = run_replay_dry_run_v2(
        feature_parquet=features_path,
        model_package=model_package,
        thresholds=thresholds,
        output_jsonl=decisions_path,
        mode="dual",
        limit=None,
        max_hold_minutes=5,
        confidence_buffer=0.05,
    )

    decisions_df = load_decisions_jsonl(decisions_path)
    threshold_payload = json.loads(threshold_report_path.read_text(encoding="utf-8"))
    default_cost = float((threshold_payload.get("decision_config") or {}).get("cost_per_trade", thresholds.cost_per_trade))
    t19_payload = (
        json.loads(t19_report_path.read_text(encoding="utf-8"))
        if isinstance(t19_report_path, Path) and t19_report_path.exists()
        else None
    )
    profile = _profile_from_t19(t19_payload=t19_payload, default_cost=default_cost)
    trades_df, replay_report = evaluate_replay(
        decisions_df=decisions_df,
        labeled_df=labeled,
        ce_threshold=float(thresholds.ce),
        pe_threshold=float(thresholds.pe),
        profile=profile,
        fill_model_config=FillModelConfig(model="constant", constant_slippage=0.0),
    )
    trades_df.to_parquet(trades_path, index=False)
    report_path.write_text(json.dumps(replay_report, indent=2), encoding="utf-8")

    out = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "date": day,
            "instrument": instrument,
            "source_requested": source,
            "source_used": resolved_source,
            "model_package": str(model_package_path),
            "threshold_report": str(threshold_report_path),
            "ce_threshold": float(thresholds.ce),
            "pe_threshold": float(thresholds.pe),
            "vix_path": str(vix_source or ""),
        },
        "artifacts": {
            "run_dir": str(run_dir),
            "panel_parquet": str(panel_path),
            "features_parquet": str(features_path),
            "labeled_parquet": str(labeled_path),
            "decisions_jsonl": str(decisions_path),
            "replay_trades_parquet": str(trades_path),
            "replay_report_json": str(report_path),
        },
        "row_counts": {
            "panel_rows": int(len(panel)),
            "features_rows": int(len(features)),
            "labeled_rows": int(len(labeled)),
            "decisions_rows": int(len(decisions_df)),
            "trades_rows": int(len(trades_df)),
        },
        "source_meta": source_meta,
        "replay_summary": replay_summary,
        "replay_report": replay_report,
    }
    full_report_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    out["artifacts"]["full_report_json"] = str(full_report_path)
    return out


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one-date model backtest from local archive or Mongo raw payloads")
    parser.add_argument("--date", required=True, help="Trade date YYYY-MM-DD (IST)")
    parser.add_argument("--instrument", required=True, help="Instrument symbol (for example BANKNIFTY26MARFUT)")
    parser.add_argument("--model-package", required=True, help="Model package joblib path")
    parser.add_argument("--threshold-report", required=True, help="Threshold report JSON path")
    parser.add_argument("--ce-threshold", type=float, default=None, help="Optional CE probability threshold override (0-1)")
    parser.add_argument("--pe-threshold", type=float, default=None, help="Optional PE probability threshold override (0-1)")
    parser.add_argument("--source", default="auto", choices=["auto", "local", "mongo"])
    parser.add_argument("--base-path", default=None, help="Optional local historical archive base path")
    parser.add_argument("--mongo-uri", default=None, help="Mongo URI override (defaults to env MONGODB_URI)")
    parser.add_argument("--mongo-db", default=None, help="Mongo DB override (defaults to env MONGO_DB)")
    parser.add_argument("--vix-path", default=None, help="Optional VIX file/dir for feature enrichment")
    parser.add_argument("--t19-report", default=None, help="Optional strategy comparison report for replay profile")
    parser.add_argument("--out-dir", default="ml_pipeline/artifacts/backtest_runs", help="Output directory")
    parser.add_argument("--tag", default=None, help="Optional run tag")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = run_date_backtest(
            day=args.date,
            instrument=args.instrument,
            model_package_path=Path(args.model_package),
            threshold_report_path=Path(args.threshold_report),
            out_dir=Path(args.out_dir),
            source=args.source,
            base_path=args.base_path,
            mongo_uri=args.mongo_uri,
            mongo_db=args.mongo_db,
            vix_path=args.vix_path,
            t19_report_path=Path(args.t19_report) if args.t19_report else None,
            tag=args.tag,
            ce_threshold_override=args.ce_threshold,
            pe_threshold_override=args.pe_threshold,
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    print(json.dumps(result, indent=2))
    print(f"FULL_REPORT={result.get('artifacts', {}).get('full_report_json', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

