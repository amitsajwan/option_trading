"""Golden-master: replay one (or more) days through the entry engine TWICE —
v1 (STRATEGY_ENTRY_PIPELINE_V2=0) and v2 (=1) — and diff the resulting trades.

This is the §6/§7 safety gate from docs/ENTRY_PIPELINE_REFACTOR.md: prove that the
new gate-cascade pipeline either matches v1 bar-for-bar, or that every divergence is
understood and intended, BEFORE flipping v2 on in live.

Run on the VM, inside the strategy_app/dashboard container (so live env + model
paths + ML libs are already set):

    python3 /tmp/golden_master_v1_v2.py                  # today
    python3 /tmp/golden_master_v1_v2.py 2026-06-02       # one day
    python3 /tmp/golden_master_v1_v2.py 2026-05-28 2026-06-02   # date range (inclusive)

Only env var toggled between the two runs is STRATEGY_ENTRY_PIPELINE_V2. Everything
else (models, thresholds, strike config, exit mode) comes from the live container env,
so the comparison reflects what live would actually do.

Exit code: 0 if v1==v2 on every compared day, 1 if any divergence (so it can gate CI).
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path("/app")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Sim hygiene — never touch live state, never publish. Do NOT set the V2 flag here;
# the harness sets it per-run. Use setdefault so the container's live values win.
os.environ["STRATEGY_RUN_DIR"] = "/tmp/golden_master_run"
os.environ.setdefault("STRATEGY_REDIS_PUBLISH_ENABLED", "0")
os.environ.setdefault("MARKET_SESSION_ENABLED", "0")
os.environ.setdefault("DEPTH_FEED_ENABLED", "0")
os.environ.setdefault("BRAIN_ENABLED", "false")
os.environ.setdefault("STRATEGY_STARTUP_WARMUP_EVENTS", "0")
Path("/tmp/golden_master_run").mkdir(exist_ok=True)

EVENTS_PATH = REPO / ".run/snapshot_app/events.jsonl"


def _load_snapshots(trade_date: str) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    snaps = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            snap = d.get("snapshot", d)
            if str(snap.get("trade_date", "")).startswith(trade_date):
                snaps.append(snap)
        except Exception:
            pass
    return snaps


def _replay_with_flag(snaps: list[dict], trade_date: str, v2: bool) -> dict:
    """Run replay_day with STRATEGY_ENTRY_PIPELINE_V2 forced to v2, restoring env after."""
    from strategy_app.sim.replay_engine import replay_day

    key = "STRATEGY_ENTRY_PIPELINE_V2"
    old = os.environ.get(key)
    os.environ[key] = "1" if v2 else "0"
    try:
        return replay_day(snaps, trade_date)
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def _trade_key(t: dict) -> tuple:
    """Identity of a trade for diffing: entry bar + direction + strike + rounded premium."""
    return (
        str(t.get("time_in")),
        str(t.get("direction")),
        t.get("strike"),
        round(float(t.get("prem_in") or 0), 1),
    )


def _diff_day(trade_date: str) -> bool:
    """Return True if v1 == v2 for this day, False on any divergence."""
    snaps = _load_snapshots(trade_date)
    if not snaps:
        print(f"  {trade_date}: no snapshots, skipped")
        return True

    r1 = _replay_with_flag(snaps, trade_date, v2=False)
    r2 = _replay_with_flag(snaps, trade_date, v2=True)
    t1, t2 = r1["trades"], r2["trades"]

    k1 = {_trade_key(t): t for t in t1}
    k2 = {_trade_key(t): t for t in t2}
    only_v1 = [k1[k] for k in k1.keys() - k2.keys()]
    only_v2 = [k2[k] for k in k2.keys() - k1.keys()]

    pnl1 = sum(float(t["pnl_pct"]) for t in t1)
    pnl2 = sum(float(t["pnl_pct"]) for t in t2)

    match = not only_v1 and not only_v2
    flag = "MATCH" if match else "DIVERGE"
    print(f"  {trade_date}: [{flag}] "
          f"v1={len(t1)} trades pnl={pnl1*100:+.2f}%  |  "
          f"v2={len(t2)} trades pnl={pnl2*100:+.2f}%  "
          f"(diag v1 entries={r1['diag']['entries']} v2 entries={r2['diag']['entries']})")

    for t in only_v1:
        print(f"      only in v1: {t['time_in']} {t['direction']} {t['strike']} "
              f"@{t['prem_in']:.0f} pnl={t['pnl_pct']*100:+.2f}% exit={t['exit']}")
    for t in only_v2:
        print(f"      only in v2: {t['time_in']} {t['direction']} {t['strike']} "
              f"@{t['prem_in']:.0f} pnl={t['pnl_pct']*100:+.2f}% exit={t['exit']}")
    return match


def _date_range(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args:
        days = [date.today().isoformat()]
    elif len(args) == 1:
        days = [args[0]]
    else:
        days = _date_range(args[0], args[1])

    print(f"Golden-master v1 vs v2 over {len(days)} day(s)\n" + "=" * 78)
    all_match = True
    for d in days:
        if not _diff_day(d):
            all_match = False
    print("=" * 78)
    print("RESULT:", "ALL MATCH — v2 is decision-equivalent to v1" if all_match
          else "DIVERGENCES FOUND — review each above before flipping v2 on")
    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
