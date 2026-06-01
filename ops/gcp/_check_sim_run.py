#!/usr/bin/env python3
"""Inspect sim run profile and PnL on VM."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else ""
if not RUN_ID:
    raise SystemExit("usage: _check_sim_run.py <run_id>")

root = Path("/opt/option_trading/.run/strategy_app_sim") / RUN_ID
rc = root / "runtime_config.json"
if rc.is_file():
    cfg = json.loads(rc.read_text(encoding="utf-8"))
    print("runtime_config.strategy_profile_id:", cfg.get("strategy_profile_id"))
else:
    print("runtime_config: missing")

with urllib.request.urlopen(f"http://127.0.0.1:8008/api/sim/runs/{RUN_ID}", timeout=15) as resp:
    print("api:", resp.read().decode("utf-8"))

pos = root / "positions.jsonl"
if not pos.is_file():
    print("positions: none yet")
    raise SystemExit(0)

rows = [json.loads(line) for line in pos.read_text(encoding="utf-8").splitlines() if line.strip()]
closes = [r for r in rows if str(r.get("event")) == "POSITION_CLOSE"]
pnl = [float(r["pnl_pct"]) for r in closes if r.get("pnl_pct") is not None]
sides = sorted({str(r.get("position_side")) for r in closes})
strategies = sorted({str(r.get("strategy")) for r in closes if r.get("strategy")})
wins = sum(1 for x in pnl if x > 0)
gross_pos = sum(x for x in pnl if x > 0)
gross_neg = -sum(x for x in pnl if x < 0)
pf = (gross_pos / gross_neg) if gross_neg > 0 else None
print("closes", len(closes), "sides", sides, "strategies", strategies)
print("net_pct", round(sum(pnl), 4) if pnl else None)
print("win_rate", round((wins / len(pnl)) * 100, 1) if pnl else None)
print("pf", round(pf, 3) if pf is not None else None)

votes = root / "votes.jsonl"
if votes.is_file():
    vrows = [json.loads(line) for line in votes.read_text(encoding="utf-8").splitlines() if line.strip()]
    vstrats = sorted({str(r.get("strategy")) for r in vrows if r.get("strategy")})
    print("vote_strategies", vstrats[:8])
