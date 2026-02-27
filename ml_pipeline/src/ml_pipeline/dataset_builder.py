import argparse
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .depth_dataset import DEPTH_COLUMNS
from .raw_loader import DayRawData, filter_valid_options, load_day_raw_data
from .schema_validator import DEFAULT_REPRESENTATIVE_DAYS, resolve_archive_base


def infer_strike_step(strikes: pd.Series) -> int:
    clean = sorted({int(x) for x in pd.to_numeric(strikes, errors="coerce").dropna().tolist()})
    if len(clean) < 2:
        return 100
    diffs = [b - a for a, b in zip(clean[:-1], clean[1:]) if (b - a) > 0]
    if not diffs:
        return 100
    mode = pd.Series(diffs).mode()
    return int(mode.iloc[0]) if not mode.empty else int(np.median(diffs))


def round_to_step(value: float, step: int) -> int:
    if step <= 0:
        return int(round(value))
    return int(round(value / step) * step)


def _minute_lookup(options: pd.DataFrame) -> Tuple[Dict[pd.Timestamp, Dict[Tuple[int, str], Dict[str, float]]], Dict[pd.Timestamp, Dict[str, float]], Dict[pd.Timestamp, str]]:
    by_minute: Dict[pd.Timestamp, Dict[Tuple[int, str], Dict[str, float]]] = {}
    aggregates: Dict[pd.Timestamp, Dict[str, float]] = {}
    expiry_hint: Dict[pd.Timestamp, str] = {}
    if options.empty:
        return by_minute, aggregates, expiry_hint

    for ts, group in options.groupby("timestamp", sort=True):
        slot: Dict[Tuple[int, str], Dict[str, float]] = {}
        for row in group.itertuples(index=False):
            key = (int(row.strike), str(row.option_type))
            slot[key] = {
                "open": float(row.open) if pd.notna(row.open) else np.nan,
                "high": float(row.high) if pd.notna(row.high) else np.nan,
                "low": float(row.low) if pd.notna(row.low) else np.nan,
                "close": float(row.close) if pd.notna(row.close) else np.nan,
                "oi": float(row.oi) if pd.notna(row.oi) else np.nan,
                "volume": float(row.volume) if pd.notna(row.volume) else np.nan,
            }
        by_minute[ts] = slot
        ce = group[group["option_type"] == "CE"]
        pe = group[group["option_type"] == "PE"]
        ce_oi = float(ce["oi"].sum()) if not ce.empty else 0.0
        pe_oi = float(pe["oi"].sum()) if not pe.empty else 0.0
        aggregates[ts] = {
            "ce_oi_total": ce_oi,
            "pe_oi_total": pe_oi,
            "ce_volume_total": float(ce["volume"].sum()) if not ce.empty else 0.0,
            "pe_volume_total": float(pe["volume"].sum()) if not pe.empty else 0.0,
            "pcr_oi": (pe_oi / ce_oi) if ce_oi > 0 else np.nan,
            "options_rows": float(len(group)),
        }
        non_null_expiry = group["expiry_code"].dropna()
        expiry_hint[ts] = str(non_null_expiry.iloc[0]) if not non_null_expiry.empty else ""

    return by_minute, aggregates, expiry_hint


def _build_row(
    fut_row: pd.Series,
    spot_row: Optional[pd.Series],
    option_slot: Dict[Tuple[int, str], Dict[str, float]],
    option_agg: Dict[str, float],
    strike_step: int,
    expiry_hint: str,
    depth_payload: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    fut_close = float(fut_row["close"])
    atm_strike = round_to_step(fut_close, strike_step)
    row: Dict[str, object] = {
        "timestamp": fut_row["timestamp"],
        "trade_date": str(pd.Timestamp(fut_row["timestamp"]).date()),
        "fut_symbol": fut_row.get("symbol"),
        "fut_open": float(fut_row["open"]),
        "fut_high": float(fut_row["high"]),
        "fut_low": float(fut_row["low"]),
        "fut_close": fut_close,
        "fut_oi": float(fut_row["oi"]),
        "fut_volume": float(fut_row["volume"]),
        "spot_open": float(spot_row["open"]) if spot_row is not None and pd.notna(spot_row["open"]) else np.nan,
        "spot_high": float(spot_row["high"]) if spot_row is not None and pd.notna(spot_row["high"]) else np.nan,
        "spot_low": float(spot_row["low"]) if spot_row is not None and pd.notna(spot_row["low"]) else np.nan,
        "spot_close": float(spot_row["close"]) if spot_row is not None and pd.notna(spot_row["close"]) else np.nan,
        "expiry_code": expiry_hint,
        "strike_step": strike_step,
        "atm_strike": atm_strike,
        "ce_oi_total": option_agg.get("ce_oi_total", np.nan),
        "pe_oi_total": option_agg.get("pe_oi_total", np.nan),
        "ce_volume_total": option_agg.get("ce_volume_total", np.nan),
        "pe_volume_total": option_agg.get("pe_volume_total", np.nan),
        "pcr_oi": option_agg.get("pcr_oi", np.nan),
        "options_rows": option_agg.get("options_rows", np.nan),
    }

    for rel, rel_name in ((-1, "m1"), (0, "0"), (1, "p1")):
        strike = atm_strike + (rel * strike_step)
        row[f"strike_{rel_name}"] = strike
        for otype, side in (("CE", "ce"), ("PE", "pe")):
            payload = option_slot.get((strike, otype))
            for field in ("open", "high", "low", "close", "oi", "volume"):
                key = f"opt_{rel_name}_{side}_{field}"
                row[key] = payload[field] if payload else np.nan

    if depth_payload is not None:
        for col in DEPTH_COLUMNS:
            row[col] = depth_payload.get(col, np.nan)

    return row


def _normalize_depth_frame(depth_frame: pd.DataFrame) -> pd.DataFrame:
    out = depth_frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    if getattr(out["timestamp"].dt, "tz", None) is not None:
        # Canonical panel timestamps are naive market-time values.
        # Drop tz info without shifting wall-clock to preserve minute alignment.
        out["timestamp"] = out["timestamp"].dt.tz_localize(None)
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    keep_cols = ["timestamp", "trade_date", *[c for c in DEPTH_COLUMNS if c in out.columns]]
    # preserve deterministic schema and order for joins
    for col in DEPTH_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    keep_cols = ["timestamp", "trade_date", *DEPTH_COLUMNS]
    out = out.loc[:, [c for c in keep_cols if c in out.columns]]
    out = out.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    return out


def build_canonical_day_panel(raw: DayRawData, depth_frame: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    fut = raw.fut.copy()
    spot = raw.spot.copy()
    options = filter_valid_options(raw.options)
    fut = fut.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    spot = spot.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    options = options.dropna(subset=["timestamp"]).sort_values(["timestamp", "symbol"]).reset_index(drop=True)

    strike_step = infer_strike_step(options["strike"]) if not options.empty else 100

    spot_lookup = {row.timestamp: row for row in spot.itertuples(index=False)}
    option_lookup, option_agg, option_expiry = _minute_lookup(options)
    depth_lookup: Optional[Dict[pd.Timestamp, Dict[str, float]]] = None
    if depth_frame is not None and len(depth_frame) > 0:
        norm_depth = _normalize_depth_frame(depth_frame)
        depth_lookup = {}
        for row in norm_depth.to_dict(orient="records"):
            ts = pd.Timestamp(row["timestamp"])
            depth_lookup[ts] = {col: float(row[col]) if pd.notna(row[col]) else np.nan for col in DEPTH_COLUMNS}

    rows: List[Dict[str, object]] = []
    for fut_row in fut.to_dict(orient="records"):
        ts = fut_row["timestamp"]
        spot_row = None
        spot_tuple = spot_lookup.get(ts)
        if spot_tuple is not None:
            spot_row = pd.Series(spot_tuple._asdict())
        slot = option_lookup.get(ts, {})
        agg = option_agg.get(ts, {})
        expiry = option_expiry.get(ts, "")
        depth_payload = depth_lookup.get(ts) if depth_lookup is not None else None
        rows.append(_build_row(pd.Series(fut_row), spot_row, slot, agg, strike_step, expiry, depth_payload=depth_payload))

    panel = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    panel["timestamp"] = pd.to_datetime(panel["timestamp"], errors="coerce")
    return panel


def build_canonical_dataset(base_path: Path, days: Sequence[str], depth_frame: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    norm_depth = _normalize_depth_frame(depth_frame) if depth_frame is not None and len(depth_frame) > 0 else None
    for day in days:
        raw = load_day_raw_data(base_path=base_path, day=day)
        day_depth = None
        if norm_depth is not None:
            day_depth = norm_depth[norm_depth["trade_date"].astype(str) == str(day)].copy()
        panel = build_canonical_day_panel(raw, depth_frame=day_depth)
        panel["source_day"] = day
        frames.append(panel)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def _split_days(raw_days: Optional[str]) -> List[str]:
    if not raw_days:
        return list(DEFAULT_REPRESENTATIVE_DAYS)
    return [item.strip() for item in raw_days.split(",") if item.strip()]


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build canonical minute dataset for ML pipeline")
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument("--days", default=None, help="Comma separated days YYYY-MM-DD")
    parser.add_argument(
        "--out",
        default="ml_pipeline/artifacts/t03_canonical_panel.parquet",
        help="Output parquet path",
    )
    parser.add_argument(
        "--depth-parquet",
        default=None,
        help="Optional depth parquet from live events (timestamp + depth_* columns)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    base = resolve_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: archive base path not found")
        return 2
    days = _split_days(args.days)
    if not days:
        print("ERROR: no days provided")
        return 2

    depth_frame = None
    if args.depth_parquet:
        depth_path = Path(args.depth_parquet)
        if not depth_path.exists():
            print(f"ERROR: depth parquet not found: {depth_path}")
            return 2
        depth_frame = pd.read_parquet(depth_path)
    dataset = build_canonical_dataset(base_path=base, days=days, depth_frame=depth_frame)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_parquet(out_path, index=False)

    print(f"Base path: {base}")
    print(f"Days: {len(days)}")
    print(f"Rows: {len(dataset)}")
    print(f"Columns: {len(dataset.columns)}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
