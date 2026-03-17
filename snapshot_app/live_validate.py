"""Validate live MarketSnapshot events written to JSONL."""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any

import pandas as pd

from snapshot_app.market_snapshot_contract import validate_market_snapshot


def _read_lines(path: Path, tail: int = 0) -> list[str]:
    if tail <= 0:
        return path.read_text(encoding="utf-8").splitlines()
    holder: deque[str] = deque(maxlen=tail)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            holder.append(line.rstrip("\n"))
    return list(holder)


def _extract_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    return payload if isinstance(payload, dict) else {}


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
        if isinstance(snapshot, dict) and snapshot:
            rows.append(snapshot)

    if not rows:
        print("ERROR: no valid snapshot rows parsed")
        return 1

    frame = pd.DataFrame(rows)
    print(f"[live-validate] Rows parsed: {len(frame)}")
    if bad_lines:
        print(f"[live-validate] Ignored malformed lines: {bad_lines}")

    if "timestamp" in frame.columns:
        ts = pd.to_datetime(frame["timestamp"], errors="coerce")
        print(f"[live-validate] Time range: {ts.min()} -> {ts.max()}")

    if "schema_name" in frame.columns:
        names = sorted({str(x) for x in frame["schema_name"].dropna().astype(str).tolist()})
        print(f"[live-validate] schema_name values: {names}")

    reports = [validate_market_snapshot(snapshot, raise_on_error=False) for snapshot in rows]
    error_reports = [report for report in reports if not bool(report.get("ok"))]
    print(f"[live-validate] Contract ok: {len(error_reports) == 0}")
    print(f"[live-validate] Invalid snapshots: {len(error_reports)}")
    for report in error_reports[:5]:
        for err in list(report.get("errors") or [])[:5]:
            print(f"  - {err}")

    print("\n[live-validate] Block presence")
    block_names = [
        "session_context",
        "futures_bar",
        "futures_derived",
        "mtf_derived",
        "opening_range",
        "vix_context",
        "chain_aggregates",
        "ladder_aggregates",
        "atm_options",
        "iv_derived",
        "session_levels",
    ]
    total = float(len(rows))
    for block_name in block_names:
        present = sum(1 for snapshot in rows if isinstance(snapshot.get(block_name), dict))
        pct = (float(present) / total) * 100.0 if total > 0 else 0.0
        print(f"  {block_name:<20} {pct:6.2f}%")

    return 0 if not error_reports else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate live MarketSnapshot JSONL against the final snapshot contract.")
    parser.add_argument("--events-path", default=".run/snapshot_app/events.jsonl")
    parser.add_argument("--tail", type=int, default=0, help="Only validate last N lines")
    args = parser.parse_args()
    return validate_live_events(events_path=Path(args.events_path), tail=max(0, int(args.tail)))


if __name__ == "__main__":
    raise SystemExit(main())
