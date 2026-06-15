#!/usr/bin/env python3
"""Keep the depth collector pointed at the CURRENT ATM strike (no python deps).

Depth is a LIVE direction input (DEPTH_FEED_ENABLED=1), so depth:atm_*:latest must
hold the current ATM strike's book. ATM drifts intraday (~1 strike/30min). Runs on
a market-hours cron: read current ATM + expiry from the latest snapshot via mongosh,
and if DEPTH_FEED_INSTRUMENTS points at a different strike, rewrite it + recreate
the collector. Idempotent (acts only when the strike changed). Runs as root (cron).
"""
import re, subprocess, sys

ENV = "/opt/option_trading/.env.compose"
COMPOSE_DIR = "/opt/option_trading"

def _sh(args):
    return subprocess.run(args, capture_output=True, text=True, cwd=COMPOSE_DIR).stdout.strip()

def latest_atm_and_expiry():
    out = _sh(["docker", "exec", "option_trading-mongo-1", "mongosh", "--quiet",
               "trading_ai", "--eval",
               'var d=db.phase1_market_snapshots.find().sort({_id:-1}).limit(1).next();'
               'print(Math.round(d.payload.snapshot.chain_aggregates.atm_strike)+"|"+d.instrument)'])
    line = [l for l in out.splitlines() if "|" in l]
    if not line:
        return None, None
    atm_s, instr = line[-1].split("|", 1)
    m = re.search(r"BANKNIFTY(\d{2}[A-Z]{3})", instr)
    return int(float(atm_s)), (m.group(1) if m else None)

def current_strike_in_env():
    for l in open(ENV):
        if l.startswith("DEPTH_FEED_INSTRUMENTS="):
            m = re.search(r"BANKNIFTY\d{2}[A-Z]{3}(\d+)CE", l)
            return int(m.group(1)) if m else None
    return None

def main():
    atm, expiry = latest_atm_and_expiry()
    if not atm or not expiry:
        print("no atm/expiry derivable; skip"); return 0
    cur = current_strike_in_env()
    if cur == atm:
        print(f"ATM unchanged ({atm}); no-op"); return 0
    inst = f"NFO:BANKNIFTY{expiry}{atm}CE,NFO:BANKNIFTY{expiry}{atm}PE"
    lines = open(ENV).read().splitlines()
    hit = False
    for i, l in enumerate(lines):
        if l.startswith("DEPTH_FEED_INSTRUMENTS="):
            lines[i] = "DEPTH_FEED_INSTRUMENTS=" + inst; hit = True
    if not hit:
        lines.append("DEPTH_FEED_INSTRUMENTS=" + inst)
    open(ENV, "w").write("\n".join(lines) + "\n")
    print(f"ATM {cur} -> {atm}; set {inst}; recreating collector")
    subprocess.run(["docker", "compose", "--env-file", ".env.compose",
                    "up", "-d", "--no-deps", "depth_collector"], cwd=COMPOSE_DIR, check=False)
    return 0

if __name__ == "__main__":
    sys.exit(main())
