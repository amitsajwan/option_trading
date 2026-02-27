import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


DEPTH_COLUMNS: Sequence[str] = (
    "depth_total_bid_qty",
    "depth_total_ask_qty",
    "depth_top_bid_qty",
    "depth_top_ask_qty",
    "depth_top_bid_price",
    "depth_top_ask_price",
    "depth_spread",
    "depth_imbalance",
)


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _parse_jsonl(path: Path) -> List[Dict[str, object]]:
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
    return rows


def build_depth_dataset_from_events(events: List[Dict[str, object]]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(columns=["timestamp", "trade_date", *DEPTH_COLUMNS])

    out_rows: List[Dict[str, object]] = []
    for event in events:
        depth = event.get("depth")
        if not isinstance(depth, dict):
            continue
        ts = pd.to_datetime(event.get("timestamp"), errors="coerce")
        if pd.isna(ts):
            continue
        row: Dict[str, object] = {
            "timestamp": pd.Timestamp(ts),
            "trade_date": str(pd.Timestamp(ts).date()),
        }
        row["depth_total_bid_qty"] = _safe_float(depth.get("total_bid_qty"))
        row["depth_total_ask_qty"] = _safe_float(depth.get("total_ask_qty"))
        row["depth_top_bid_qty"] = _safe_float(depth.get("top_bid_qty"))
        row["depth_top_ask_qty"] = _safe_float(depth.get("top_ask_qty"))
        row["depth_top_bid_price"] = _safe_float(depth.get("top_bid_price"))
        row["depth_top_ask_price"] = _safe_float(depth.get("top_ask_price"))
        row["depth_spread"] = _safe_float(depth.get("spread"))
        row["depth_imbalance"] = _safe_float(depth.get("imbalance"))
        out_rows.append(row)

    if not out_rows:
        return pd.DataFrame(columns=["timestamp", "trade_date", *DEPTH_COLUMNS])

    frame = pd.DataFrame(out_rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    # Keep latest row per minute timestamp for deterministic joins.
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    return frame


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build minute depth dataset from decision event JSONL")
    parser.add_argument(
        "--events-jsonl",
        default="ml_pipeline/artifacts/t30_live_redis_v2_events.jsonl",
        help="Input decision events JSONL containing depth payload",
    )
    parser.add_argument(
        "--out",
        default="ml_pipeline/artifacts/t31_depth_dataset.parquet",
        help="Output depth parquet",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    in_path = Path(args.events_jsonl)
    if not in_path.exists():
        print(f"ERROR: events jsonl not found: {in_path}")
        return 2

    events = _parse_jsonl(in_path)
    depth_df = build_depth_dataset_from_events(events)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    depth_df.to_parquet(out_path, index=False)

    print(f"Input events: {len(events)}")
    print(f"Depth rows: {len(depth_df)}")
    print(f"Columns: {len(depth_df.columns)}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
