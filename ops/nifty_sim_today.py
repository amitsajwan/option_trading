"""
NIFTY SIM — replay today's NIFTY snapshots with the current live engine config.

Run inside strategy_app_nifty container:
  docker exec option_trading-strategy_app_nifty-1 python3 /tmp/nifty_sim_today.py

The container already carries correct env vars for NIFTY
(ENTRY_ML_MODEL_PATH, DIRECTION_ML_MODEL_PATH, ENTRY_ML_MAX_NAN_FEATURES, etc.).
This script only overrides the vars that differ between live and sim.
"""
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

# ── Sim-only overrides (do NOT touch model/gate vars — use container's values) ─
os.environ["STRATEGY_RUN_DIR"]             = "/tmp/sim_nifty"
os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
os.environ["MARKET_SESSION_ENABLED"]       = "0"
os.environ["BRAIN_ENABLED"]                = "false"
os.environ["DEPTH_FEED_ENABLED"]           = "0"
os.environ.setdefault("STRATEGY_STARTUP_WARMUP_EVENTS", "0")
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("strategy_app").setLevel(logging.WARNING)
logging.getLogger("contracts_app").setLevel(logging.WARNING)
logging.getLogger("snapshot_app").setLevel(logging.WARNING)

sys.path.insert(0, "/app")
Path("/tmp/sim_nifty").mkdir(exist_ok=True)

from strategy_app.engines import DeterministicRuleEngine
from strategy_app.contracts import SignalType

TODAY   = date.today().isoformat()
EVENTS  = Path("/app/.run/snapshot_app_nifty/events.jsonl")
PROFILE = os.environ.get("STRATEGY_PROFILE_ID", "trader_master_ml_entry_v1")

print("=" * 70)
print(f"NIFTY SIM — {TODAY}")
print("-" * 70)
print(f"  profile:        {PROFILE}")
print(f"  entry_model:    {os.environ.get('ENTRY_ML_MODEL_PATH','NOT SET')}")
print(f"  direction_model:{os.environ.get('DIRECTION_ML_MODEL_PATH','NOT SET')}")
print(f"  min_prob:       {os.environ.get('ENTRY_ML_MIN_PROB','NOT SET')}")
print(f"  entry_nan_gate: {os.environ.get('ENTRY_ML_MAX_NAN_FEATURES','NOT SET')}")
print(f"  dir_nan_gate:   {os.environ.get('DIRECTION_ML_MAX_NAN_FEATURES','NOT SET')}")
print(f"  dir_mode:       {os.environ.get('ML_ENTRY_DIRECTION_MODE','NOT SET')}")
print("=" * 70)

# ── Load engine ──────────────────────────────────────────────────────────────
try:
    engine = DeterministicRuleEngine(
        min_confidence=float(os.environ.get("STRATEGY_MIN_CONFIDENCE", "0.05")),
        strategy_profile_id=PROFILE,
    )
    print(f"\nEngine loaded OK — profile={PROFILE}")
except Exception as exc:
    print(f"\nEngine load FAILED: {exc}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# ── Load today's NIFTY snapshots ─────────────────────────────────────────────
if not EVENTS.exists():
    print(f"ERROR: events file not found: {EVENTS}")
    sys.exit(1)

snapshots = []
for line in EVENTS.read_text().splitlines():
    try:
        d    = json.loads(line)
        snap = d.get("snapshot", d)
        ts   = str(snap.get("trade_date", snap.get("timestamp", "")))
        if ts.startswith(TODAY):
            snapshots.append(snap)
    except Exception:
        pass

print(f"Snapshots for {TODAY}: {len(snapshots)}")
if not snapshots:
    print("No snapshots — nothing to sim (market may not have been open today)")
    sys.exit(0)

# ── Replay ────────────────────────────────────────────────────────────────────
trade_date  = date.fromisoformat(TODAY)
engine.on_session_start(trade_date)

trades          = []
current_entry   = None
entry_decision_traces = []  # keep last few for inspection

print(f"\nReplaying {len(snapshots)} bars...\n")

for snap in snapshots:
    try:
        signal = engine.evaluate(snap)
    except Exception as exc:
        sid = snap.get("snapshot_id", "?")
        print(f"  EVAL ERROR {sid}: {exc}")
        continue

    if signal is None:
        continue

    ts   = str(snap.get("timestamp", ""))
    hhmm = ts[11:16] if len(ts) > 15 else "?"

    if signal.signal_type == SignalType.ENTRY:
        dm  = signal.decision_metrics or {}
        print(f"  ENTRY {hhmm}  {str(getattr(signal.direction,'value',signal.direction)):2s}  "
              f"strike={signal.strike}  prem={float(signal.entry_premium or 0):.0f}  "
              f"conf={dm.get('confidence','?')}  prob={dm.get('entry_prob','?')}  "
              f"dir_ce_prob={dm.get('ce_prob','?')}")
        current_entry = {
            "time_in":  hhmm,
            "dir":      str(getattr(signal.direction, "value", signal.direction)),
            "strike":   signal.strike,
            "prem_in":  float(signal.entry_premium or 0),
        }
        entry_decision_traces.append(dm)

    elif signal.signal_type == SignalType.EXIT and current_entry is not None:
        closed = engine._tracker._closed_positions
        if closed:
            cp       = closed[-1]
            pnl_pct  = float(cp.get("pnl_pct", 0))
            mfe_pct  = float(cp.get("mfe_pct", 0))
            mae_pct  = float(cp.get("mae_pct", 0))
            exit_prem = float(cp.get("exit_premium", current_entry["prem_in"]))
        else:
            pnl_pct = mfe_pct = mae_pct = 0.0
            exit_prem = current_entry["prem_in"]

        er    = signal.exit_reason.value if signal.exit_reason else "?"
        ep    = (signal.decision_metrics or {}).get("exit_policy_triggered", "")
        label = ep or er
        sign  = "+" if pnl_pct >= 0 else ""
        print(f"  EXIT  {hhmm}  pnl={sign}{pnl_pct*100:.2f}%  "
              f"mfe={mfe_pct*100:.2f}%  mae={mae_pct*100:.2f}%  "
              f"prem_out={exit_prem:.0f}  via={label}")

        trades.append({"pnl": pnl_pct, "mfe": mfe_pct, "mae": mae_pct,
                       "dir": current_entry["dir"], "exit": label})
        current_entry = None

engine.on_session_end(trade_date)

# ── Session summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"  Trades: {len(trades)}")
if trades:
    pnls = [t["pnl"] for t in trades]
    mfes = [t["mfe"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    cap_num = sum(p for p, m in zip(pnls, mfes) if m > 0)
    cap_den = sum(m for m in mfes if m > 0)
    cap = cap_num / cap_den if cap_den else 0.0
    print(f"  Wins:         {wins}/{len(trades)} ({wins*100//len(trades)}%)")
    print(f"  Session P&L:  {sum(pnls)*100:+.2f}%")
    print(f"  Avg MFE:      {sum(mfes)*100/len(mfes):+.2f}%")
    print(f"  Capture:      {cap*100:.0f}%")
    print(f"  Exit reasons: {sorted(set(t['exit'] for t in trades))}")
else:
    print("  No trades fired")

# ── Trace inspection — top-level keys + last entry decision metrics ───────────
print("\n--- Decision trace sample (last entry) ---")
if entry_decision_traces:
    last = entry_decision_traces[-1]
    for k, v in sorted(last.items()):
        print(f"    {k}: {v}")
else:
    print("  No entry traces captured")

# Check decision_traces.jsonl for full trace (regime, gate results, etc.)
trace_path = Path("/tmp/sim_nifty/decision_traces.jsonl")
if trace_path.exists():
    lines = trace_path.read_text().strip().splitlines()
    print(f"\n--- decision_traces.jsonl: {len(lines)} bars ---")
    if lines:
        last_trace = json.loads(lines[-1])
        trace_payload = last_trace.get("payload", last_trace)
        tr = trace_payload.get("trace", trace_payload)
        print(f"  regime:            {(tr.get('regime_context') or {}).get('regime','?')}")
        print(f"  entry_vote:        {tr.get('entry_vote','?')}")
        print(f"  cost_gate:         {(tr.get('cost_gate') or {}).get('ok','?')}")
        print(f"  direction_source:  {tr.get('direction_source','?')}")
        print(f"  ml_entry_result:   {tr.get('ml_entry_result','?')}")
print("=" * 70)
