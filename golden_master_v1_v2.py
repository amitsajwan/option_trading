"""Golden-master: replay one (or more) days through the entry engine TWICE —
v1 (STRATEGY_ENTRY_PIPELINE_V2=0) and v2 (=1) — and diff the resulting trades,
with per-bar gate-cascade attribution for every divergent bar.

This is the §6/§7 safety gate from docs/ENTRY_PIPELINE_REFACTOR.md.

Run on the VM, inside the strategy_app container (live env + models + libs):

    python3 /tmp/golden_master_v1_v2.py 2026-05-26              # live profile
    python3 /tmp/golden_master_v1_v2.py --ops 2026-06-02        # OPS sim config (fires on quiet days)
    python3 /tmp/golden_master_v1_v2.py --ops 2026-05-26 2026-06-02   # date range

--ops applies the full OPS-sim config (consensus profile + adaptive exits + smart
strike @ ₹500) so days that are silent under the strict live profile actually fire —
the only way to compare v1 vs v2 on a representative trading day like 06-02.

Per divergent bar, prints the v2 gate cascade (which gate stopped it + the numbers),
read from engine.last_entry_trace via ReplayResult.decision_traces.

Exit 0 if v1==v2 on every compared day, 1 on any divergence.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path("/app")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Sim hygiene — never touch live state, never publish.
os.environ["STRATEGY_RUN_DIR"] = "/tmp/golden_master_run"
os.environ.setdefault("STRATEGY_REDIS_PUBLISH_ENABLED", "0")
os.environ.setdefault("MARKET_SESSION_ENABLED", "0")
os.environ.setdefault("DEPTH_FEED_ENABLED", "0")
os.environ.setdefault("BRAIN_ENABLED", "false")
os.environ.setdefault("STRATEGY_STARTUP_WARMUP_EVENTS", "0")
Path("/tmp/golden_master_run").mkdir(exist_ok=True)

EVENTS_PATH = REPO / ".run/snapshot_app/events.jsonl"

# Full OPS-sim config — mirrors market_data_dashboard/routes/ops_routes.py sim_env.
# ML model paths are left to the live container env (already set); these are the
# strategy knobs that make a quiet day fire like the OPS panel sim.
OPS_SIM_CONFIG = {
    "STRATEGY_PROFILE_ID": "trader_master_ml_entry_consensus_v1",
    "STRATEGY_MIN_CONFIDENCE": "0.80",
    "CONSENSUS_BYPASS_MIN_CONFIDENCE": "0.80",
    "EXIT_STRATEGY_MODE": "adaptive",
    "EXIT_POLICY_STACK_ENABLED": "1",
    "EXIT_PREMIUM_TARGET_PCT": "0.04",
    "EXIT_TRAILING_ACTIVATION_PCT": "0.01",
    "EXIT_TRAILING_TRAIL_PCT": "0.005",
    "STRATEGY_STRIKE_SELECTION_POLICY": "smart_strike",
    "STRATEGY_SMART_STRIKE_ENABLED": "1",
    # Soft cap on purpose: --ops reproduces the *original firing* OPS sim (ATM
    # fallback, ~15 trades) so v1-vs-v2 parity can be checked on real trades.
    # The hard-cap behaviour is verified separately (it correctly vetoes those
    # over-budget ATM trades — see ENTRY_PIPELINE_V1_V2_ANALYSIS.md).
    "SMART_STRIKE_MAX_PREMIUM": "500",
    "SMART_STRIKE_HARD_PREMIUM_CAP": "0",
    "STRATEGY_STRIKE_MAX_OTM_STEPS": "8",
    "SMART_STRIKE_OTM_CONFIDENCE": "0.55",
    "SMART_STRIKE_OTM2_ENABLED": "1", "SMART_STRIKE_OTM2_CONFIDENCE": "0.65",
    "SMART_STRIKE_OTM3_ENABLED": "1", "SMART_STRIKE_OTM3_CONFIDENCE": "0.75",
    "SMART_STRIKE_OTM3_REGIMES": "BREAKOUT,TRENDING",
    "SMART_STRIKE_OTM4_ENABLED": "1", "SMART_STRIKE_OTM4_CONFIDENCE": "0.85",
    "SMART_STRIKE_OTM4_REGIMES": "BREAKOUT",
    "SMART_STRIKE_OTM2_MIN_OI": "20000", "SMART_STRIKE_OTM3_MIN_OI": "15000",
    "SMART_STRIKE_OTM4_MIN_OI": "10000",
    "SMART_STRIKE_OTM_IV_CEIL": "92", "SMART_STRIKE_OTM2_IV_CEIL": "91",
    "SMART_STRIKE_OTM3_IV_CEIL": "90", "SMART_STRIKE_OTM4_IV_CEIL": "89",
    "ENTRY_ML_MIN_PROB": "0.65",
    "DIRECTION_ML_WEIGHT": "0.40",
    "RISK_MAX_SESSION_TRADES": "20",
    "RISK_MAX_CONSECUTIVE_LOSSES": "3",
}
# Default model paths if the container env doesn't already carry them.
_MODEL_DEFAULTS = {
    "ENTRY_ML_MODEL_PATH": "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib",
    "DIRECTION_ML_MODEL_PATH": "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib",
}


def apply_ops_config() -> None:
    for k, v in OPS_SIM_CONFIG.items():
        os.environ[k] = v
    for k, v in _MODEL_DEFAULTS.items():
        os.environ.setdefault(k, v)
    print("[--ops] applied OPS-sim config: profile=%s exit=%s max_premium=%s hard_cap=%s"
          % (OPS_SIM_CONFIG["STRATEGY_PROFILE_ID"], OPS_SIM_CONFIG["EXIT_STRATEGY_MODE"],
             OPS_SIM_CONFIG["SMART_STRIKE_MAX_PREMIUM"], OPS_SIM_CONFIG["SMART_STRIKE_HARD_PREMIUM_CAP"]))


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
    return (str(t.get("time_in")), str(t.get("direction")), t.get("strike"),
            round(float(t.get("prem_in") or 0), 1))


def _fmt_cascade(trace: dict) -> str:
    """One compact line per gate in the cascade."""
    parts = []
    for g in trace.get("gates", []):
        oc = g["outcome"]
        mark = "OK" if oc == "pass" else oc.upper()
        seg = f"{g['gate']}={mark}"
        if oc != "pass":
            seg += f"({g.get('reason', '')}"
            vals = g.get("values") or {}
            if vals:
                seg += " " + " ".join(f"{k}={v}" for k, v in vals.items())
            seg += ")"
        parts.append(seg)
    return " → ".join(parts)


def _diff_day(trade_date: str) -> bool:
    snaps = _load_snapshots(trade_date)
    if not snaps:
        print(f"  {trade_date}: no snapshots, skipped")
        return True

    r1 = _replay_with_flag(snaps, trade_date, v2=False)
    r2 = _replay_with_flag(snaps, trade_date, v2=True)
    t1, t2 = r1["trades"], r2["trades"]
    v2_traces = {str(tr.get("timestamp"))[11:16]: tr for tr in r2.get("decision_traces", [])}

    k1 = {_trade_key(t): t for t in t1}
    k2 = {_trade_key(t): t for t in t2}
    only_v1 = sorted((k1[k] for k in k1.keys() - k2.keys()), key=lambda t: t["time_in"])
    only_v2 = sorted((k2[k] for k in k2.keys() - k1.keys()), key=lambda t: t["time_in"])

    pnl1 = sum(float(t["pnl_pct"]) for t in t1)
    pnl2 = sum(float(t["pnl_pct"]) for t in t2)
    match = not only_v1 and not only_v2
    print(f"  {trade_date}: [{'MATCH' if match else 'DIVERGE'}] "
          f"v1={len(t1)} ({pnl1*100:+.2f}%)  v2={len(t2)} ({pnl2*100:+.2f}%)  "
          f"[v2 entries={r2['diag']['entries']} v2 bars_traced={len(r2.get('decision_traces', []))}]")

    # For each bar v1 traded but v2 didn't, show the v2 cascade at that bar.
    for t in only_v1:
        hhmm = t["time_in"]
        tr = v2_traces.get(hhmm)
        print(f"      v1-only {hhmm} {t['direction']} {t['strike']} @{t['prem_in']:.0f} "
              f"pnl={t['pnl_pct']*100:+.2f}%")
        if tr:
            print(f"        v2@{hhmm}: outcome={tr['final_outcome']} "
                  f"blocker={tr.get('primary_blocker_gate')}")
            print(f"        cascade: {_fmt_cascade(tr)}")
        else:
            print(f"        v2@{hhmm}: no decision trace (bar not evaluated by v2 — "
                  f"upstream short-circuit: no votes / position held / cooldown)")
    for t in only_v2:
        print(f"      v2-only {t['time_in']} {t['direction']} {t['strike']} @{t['prem_in']:.0f} "
              f"pnl={t['pnl_pct']*100:+.2f}%")

    # v2 blocker-gate histogram for the whole day (where did v2 stop?)
    hist: dict[str, int] = {}
    for tr in r2.get("decision_traces", []):
        key = tr.get("primary_blocker_gate") or tr["final_outcome"]
        hist[key] = hist.get(key, 0) + 1
    if hist:
        print("      v2 bar outcomes:", ", ".join(f"{k}={v}" for k, v in sorted(hist.items(), key=lambda x: -x[1])))
    return match


def _date_range(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--ops"]
    if "--ops" in sys.argv[1:]:
        apply_ops_config()

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
