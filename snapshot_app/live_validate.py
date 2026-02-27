"""Validate live snapshot events written to JSONL."""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any

import pandas as pd


def _read_lines(path: Path, tail: int = 0) -> list[str]:
    if tail <= 0:
        return path.read_text(encoding="utf-8").splitlines()
    holder: deque[str] = deque(maxlen=tail)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            holder.append(line.rstrip("\n"))
    return list(holder)


def _extract_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    snap = payload.get("snapshot")
    if isinstance(snap, dict):
        return snap
    return payload if isinstance(payload, dict) else {}


def _flatten_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for block in (
        "session_context",
        "futures_bar",
        "futures_derived",
        "opening_range",
        "vix_context",
        "chain_aggregates",
        "atm_options",
        "iv_derived",
        "session_levels",
    ):
        data = snapshot.get(block)
        if isinstance(data, dict):
            out.update(data)
    out["snapshot_id"] = snapshot.get("snapshot_id")
    out["instrument"] = snapshot.get("instrument")
    return out


def validate_live_events(events_path: Path, tail: int = 0) -> int:
    if not events_path.exists():
        print(f"ERROR: events file not found: {events_path}")
        return 1

    lines = _read_lines(events_path, tail=tail)
    if not lines:
        print("ERROR: events file is empty")
        return 1

    rows: list[dict[str, Any]] = []
    bad_lines = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            bad_lines += 1
            continue
        snapshot = _extract_snapshot(payload)
        if snapshot:
            rows.append(_flatten_snapshot(snapshot))

    if not rows:
        print("ERROR: no valid snapshot rows parsed")
        return 1

    df = pd.DataFrame(rows)
    n = len(df)
    print(f"[live-validate] Rows parsed: {n}")
    if bad_lines:
        print(f"[live-validate] Ignored malformed lines: {bad_lines}")

    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        print(f"[live-validate] Time range: {ts.min()} -> {ts.max()}")

    key_fields = [
        "snapshot_id",
        "minutes_since_open",
        "fut_close",
        "pcr",
        "pcr_change_30m",
        "atm_ce_oi_change_30m",
        "atm_pe_oi_change_30m",
        "atm_ce_iv",
        "atm_pe_iv",
        "iv_skew",
        "iv_percentile",
    ]
    print("\n[live-validate] Null rates")
    for field in key_fields:
        if field not in df.columns:
            print(f"  {field:<24} MISSING")
            continue
        null_pct = float(df[field].isna().sum()) / float(n) * 100.0
        print(f"  {field:<24} {null_pct:6.2f}%")

    warmup_fields = ["pcr_change_30m", "atm_ce_oi_change_30m", "atm_pe_oi_change_30m"]
    max_mso = None
    if "minutes_since_open" in df.columns:
        mso = pd.to_numeric(df["minutes_since_open"], errors="coerce")
        if mso.notna().any():
            max_mso = int(mso.max())

    all_null_warmup = True
    for field in warmup_fields:
        if field in df.columns and pd.Series(df[field]).notna().any():
            all_null_warmup = False
            break

    print("\n[live-validate] 30m-delta interpretation")
    if max_mso is None:
        print("  minutes_since_open unavailable, cannot assess warm-up behavior.")
    elif max_mso < 30:
        print("  Expected: 30m delta fields can be null before 30 minutes of session.")
    elif all_null_warmup:
        print("  WARNING: 30m delta fields are still all null after minute>=30.")
        print("  Likely causes: app started recently/intraday, state reset, or sparse event history in file.")
    else:
        print("  OK: at least some 30m delta values are populated.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate live snapshot JSONL quality.")
    parser.add_argument("--events-path", default=".run/snapshot_app/events.jsonl")
    parser.add_argument("--tail", type=int, default=0, help="Only validate last N lines")
    args = parser.parse_args()
    return validate_live_events(events_path=Path(args.events_path), tail=max(0, int(args.tail)))


if __name__ == "__main__":
    raise SystemExit(main())
