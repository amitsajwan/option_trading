#!/usr/bin/env python3
"""CLI wrapper to distil decision traces into an LLM-readable digest.

The analysis core lives in ``strategy_app.sim.trace_digest`` so it ships inside
both the strategy and dashboard images (the dashboard runs the ops-sim in-process
and auto-generates this digest). This file only adds JSONL I/O + argparse.

Inputs are JSONL files (one trace per line), so it works on both the ephemeral sim
export and the live decision-trace sink. The same functions can be imported and
called with an in-process list of traces (e.g. a finished replay's decision_traces).

Usage:
  python analyze_sim_trace.py --traces traces.jsonl [--trades trades.jsonl] \
      [--snapshots snaps.jsonl] [--entry-horizon 10] [--entry-min-points 50] \
      [--json out.json] [--md out.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make strategy_app importable when run as a standalone script on the VM.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from strategy_app.sim.trace_digest import (  # noqa: E402
    analyze_traces,
    render_markdown,
    verify_entry_label,
)

__all__ = ["analyze_traces", "render_markdown", "verify_entry_label"]


def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traces", required=True, help="JSONL of decision traces (one per line)")
    ap.add_argument("--trades", default=None, help="optional JSONL of trades/positions for win/loss join")
    ap.add_argument("--snapshots", default=None,
                    help="optional JSONL of fut snapshots (timestamp + fut_high/low/close) for entry-label verification")
    ap.add_argument("--entry-horizon", type=int, default=10, help="entry-label horizon minutes (deployed model=10)")
    ap.add_argument("--entry-min-points", type=float, default=50.0, help="entry-label move threshold in points (deployed model=50)")
    ap.add_argument("--json", dest="json_out", default=None, help="write full report JSON here")
    ap.add_argument("--md", dest="md_out", default=None, help="write markdown digest here")
    args = ap.parse_args()

    traces = _read_jsonl(args.traces)
    trades = _read_jsonl(args.trades) if args.trades else None
    snapshots = _read_jsonl(args.snapshots) if args.snapshots else None
    if not traces:
        print(f"no traces found in {args.traces}", file=sys.stderr)
        return 2

    report = analyze_traces(
        traces, trades,
        snapshots=snapshots,
        entry_horizon_minutes=args.entry_horizon,
        entry_min_points=args.entry_min_points,
    )
    md = render_markdown(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
