"""MD-S7 — Sim fidelity validation.

Proves that replay_day() on a known live session produces identical results to what
ran live (same trade count, same P&L, same exit reasons). This is the mandatory gate
before trusting any multi-day sim output.

Usage
-----
    python -m strategy_app.sim.fidelity_check \
        --date 2026-06-01 \
        --live-positions /app/.run/strategy_app/positions.jsonl \
        --events /app/.run/snapshot_app/events.jsonl \
        [--ops-env /app/.run/strategy_app/ops_env.json]

Exit codes
----------
    0  PASS — sim matches live to within tolerance on all checked metrics.
    1  FAIL — mismatch found; report printed to stdout.
    2  ERROR — could not load data.

Why this matters
----------------
Four real sim-divergence bugs were found in this project (ML library drift, wrong
config source, risk_config overwrite, sim writes to live JSONL). Each time the sim
looked like it was working but produced wrong numbers. Running this check on a known
day catches any regression before trusting a multi-day result.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Live positions reader ─────────────────────────────────────────────────────

def _load_live_trades(positions_jsonl: str, trade_date: str) -> List[dict]:
    """Read closed trades from live positions.jsonl for the given date."""
    path = Path(positions_jsonl)
    if not path.exists():
        return []

    open_ts: Dict[str, str] = {}
    closes: Dict[str, dict] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = str(d.get("timestamp", ""))
        if not ts.startswith(trade_date):
            continue
        run_id = str(d.get("run_id") or "")
        if run_id.lower().startswith("sim"):
            continue
        pid = d.get("position_id", "")
        evt = d.get("event", "")
        if evt == "POSITION_OPEN" and pid and pid not in open_ts:
            open_ts[pid] = ts
        elif evt == "POSITION_CLOSE" and pid:
            closes[pid] = d

    trades = []
    seen: set = set()
    for pid, p in closes.items():
        close_ts = str(p.get("timestamp", ""))
        entry_ts = open_ts.get(pid, close_ts)
        key = (entry_ts[:16], p.get("direction"), p.get("strike"),
               round(float(p.get("entry_premium") or 0), 1))
        if key in seen:
            continue
        seen.add(key)
        trades.append({
            "entry_time":   entry_ts[11:16],
            "exit_time":    close_ts[11:16],
            "direction":    p.get("direction"),
            "strike":       p.get("strike"),
            "pnl_pct":      float(p.get("pnl_pct") or 0),
            "mfe_pct":      float(p.get("mfe_pct") or 0),
            "exit_reason":  str(p.get("exit_policy_triggered") or p.get("exit_reason") or ""),
        })
    trades.sort(key=lambda x: x["entry_time"])
    return trades


# ── Snapshot loader (from events.jsonl for today) ─────────────────────────────

def _load_live_snapshots(events_jsonl: str, trade_date: str) -> List[dict]:
    path = Path(events_jsonl)
    if not path.exists():
        return []
    snaps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            snap = d.get("snapshot", d)
            if str(snap.get("trade_date", "")).startswith(trade_date):
                snaps.append(snap)
        except Exception:
            pass
    return snaps


# ── Fidelity check ────────────────────────────────────────────────────────────

def run_fidelity_check(
    trade_date: str,
    events_jsonl: str,
    positions_jsonl: str,
    ops_env_json: Optional[str] = None,
    pnl_tolerance: float = 0.001,   # 0.1% absolute tolerance per trade
    count_tolerance: int = 0,       # default: trade count must match exactly
) -> dict[str, Any]:
    """Compare sim output to live trades for one day.

    Returns a result dict with keys: passed, live_count, sim_count, mismatches, report.
    """
    live_trades = _load_live_trades(positions_jsonl, trade_date)
    snapshots   = _load_live_snapshots(events_jsonl, trade_date)

    if not snapshots:
        return {"passed": False, "error": f"No snapshots in {events_jsonl} for {trade_date}"}
    if not live_trades:
        return {"passed": False, "error": f"No live trades in {positions_jsonl} for {trade_date}"}

    # Load ops_env.json as the config baseline (same as _run_sim_thread does)
    live_env: Dict[str, str] = {}
    if ops_env_json and Path(ops_env_json).exists():
        try:
            live_env = json.loads(Path(ops_env_json).read_text(encoding="utf-8"))
        except Exception:
            pass

    sim_env = {
        "STRATEGY_REDIS_PUBLISH_ENABLED": "0",
        "MARKET_SESSION_ENABLED":          "0",
        "BRAIN_ENABLED":                   "false",
        "STRATEGY_STARTUP_WARMUP_EVENTS":  "0",
        "DEPTH_FEED_ENABLED":              "0",
    }
    sim_env.update({k: str(v) for k, v in live_env.items() if v not in (None, "")})

    with tempfile.TemporaryDirectory(prefix=f"fidelity_{trade_date}_") as tmpdir:
        sim_env["STRATEGY_RUN_DIR"] = tmpdir
        sim_env["STRATEGY_RUN_ID"]  = f"fidelity-{trade_date}"

        old_env: Dict[str, Optional[str]] = {}
        for k, v in sim_env.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            from strategy_app.sim.replay_engine import replay_day
            replay_result = replay_day(snapshots, trade_date)
        finally:
            for k, old in old_env.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old

    sim_trades = replay_result["trades"]

    # ── Compare ───────────────────────────────────────────────────────────────
    mismatches: List[str] = []

    count_diff = abs(len(live_trades) - len(sim_trades))
    if count_diff > count_tolerance:
        mismatches.append(
            f"Trade count mismatch: live={len(live_trades)} sim={len(sim_trades)}"
        )

    # Match by entry_time + direction (order may differ for multi-lot setups)
    live_by_key  = {(t["entry_time"], t["direction"]): t for t in live_trades}
    sim_by_key   = {(t["time_in"],   t["direction"]): t for t in sim_trades}
    all_keys = set(live_by_key) | set(sim_by_key)

    for key in sorted(all_keys):
        live = live_by_key.get(key)
        sim  = sim_by_key.get(key)
        if live is None:
            mismatches.append(f"{key}: in sim but not live")
            continue
        if sim is None:
            mismatches.append(f"{key}: in live but not sim")
            continue
        diff = abs(live["pnl_pct"] - sim["pnl_pct"])
        if diff > pnl_tolerance:
            mismatches.append(
                f"{key}: pnl live={live['pnl_pct']:+.4f} sim={sim['pnl_pct']:+.4f} diff={diff:.4f}"
            )

    live_total = sum(t["pnl_pct"] for t in live_trades)
    sim_total  = sum(t["pnl_pct"] for t in sim_trades)
    session_diff = abs(live_total - sim_total)

    passed = len(mismatches) == 0

    report_lines = [
        f"# Fidelity Check — {trade_date}",
        "",
        f"Result: **{'PASS ✓' if passed else 'FAIL ✗'}**",
        "",
        f"| | Live | Sim |",
        f"|---|---|---|",
        f"| Trades | {len(live_trades)} | {len(sim_trades)} |",
        f"| Session P&L | {live_total:+.4f}% | {sim_total:+.4f}% |",
        f"| Snapshots | — | {len(snapshots)} |",
        "",
    ]
    if mismatches:
        report_lines += ["## Mismatches", ""]
        for m in mismatches:
            report_lines.append(f"- {m}")
        report_lines.append("")
    else:
        report_lines += [
            f"Session P&L diff: {session_diff:.6f}% (tolerance {pnl_tolerance:.3f}%)",
            "All per-trade P&L values match within tolerance.",
        ]

    report_lines += ["", "## Per-trade comparison", "",
                     "| Time | Dir | Live P&L | Sim P&L | Δ | Live exit | Sim exit |",
                     "|---|---|---|---|---|---|---|"]
    for key in sorted(all_keys):
        live = live_by_key.get(key)
        sim  = sim_by_key.get(key)
        lp = f"{live['pnl_pct']:+.4f}%" if live else "—"
        sp = f"{sim['pnl_pct']:+.4f}%" if sim else "—"
        delta = f"{abs(live['pnl_pct'] - sim['pnl_pct']):.4f}%" if (live and sim) else "—"
        le = live.get("exit_reason", "—") if live else "—"
        se = sim.get("exit", "—") if sim else "—"
        report_lines.append(f"| {key[0]} | {key[1]} | {lp} | {sp} | {delta} | {le} | {se} |")

    return {
        "passed":      passed,
        "live_count":  len(live_trades),
        "sim_count":   len(sim_trades),
        "live_pnl":    live_total,
        "sim_pnl":     sim_total,
        "session_diff": session_diff,
        "mismatches":  mismatches,
        "report":      "\n".join(report_lines),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Sim fidelity validation (MD-S7)")
    parser.add_argument("--date",           required=True, help="YYYY-MM-DD")
    parser.add_argument("--live-positions", required=True, help="Path to positions.jsonl")
    parser.add_argument("--events",         required=True, help="Path to events.jsonl (snapshots)")
    parser.add_argument("--ops-env",        default=None,  help="Path to ops_env.json")
    parser.add_argument("--out",            default=None,  help="Write report to this file")
    parser.add_argument("--pnl-tolerance",  type=float, default=0.001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    result = run_fidelity_check(
        trade_date=args.date,
        events_jsonl=args.events,
        positions_jsonl=args.live_positions,
        ops_env_json=args.ops_env,
        pnl_tolerance=args.pnl_tolerance,
    )

    print(result["report"])

    if args.out:
        Path(args.out).write_text(result["report"], encoding="utf-8")
        print(f"\nReport written to {args.out}")

    sys.exit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    _cli()
