"""
Sim today's snapshots with the current live config.

Reads: /opt/option_trading/.run/snapshot_app/events.jsonl  (today's only)
Uses:  current strategy_app engine + profile + env vars
Outputs: trade-by-trade table + session summary

Run on VM:
  python3 /tmp/ops_sim_today.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import date

# ── Config: mirror live env vars ────────────────────────────────────────────
os.environ.setdefault("EXIT_POLICY_STACK_ENABLED",       "1")
os.environ.setdefault("EXIT_PREMIUM_TARGET_PCT",          "0.04")
os.environ.setdefault("EXIT_TRAILING_ACTIVATION_PCT",     "0.01")
os.environ.setdefault("EXIT_TRAILING_TRAIL_PCT",          "0.005")
os.environ.setdefault("EXIT_THESIS_FAIL_BARS",            "3")
os.environ.setdefault("EXIT_THESIS_FAIL_MIN_MFE",         "0.002")
os.environ.setdefault("CONSENSUS_BYPASS_MIN_CONFIDENCE",  "0.65")
os.environ.setdefault("DIRECTION_MIN_MARGIN_SIDEWAYS",    "2.0")
os.environ.setdefault("STRATEGY_STRIKE_SELECTION_POLICY", "smart_strike")
os.environ.setdefault("SMART_STRIKE_MAX_PREMIUM",         "800")
os.environ.setdefault("STRATEGY_STRIKE_MAX_OTM_STEPS",   "8")
os.environ.setdefault("STRATEGY_SMART_STRIKE_ENABLED",    "1")
os.environ.setdefault("SMART_STRIKE_OTM_CONFIDENCE",      "0.55")
os.environ.setdefault("SMART_STRIKE_OTM2_ENABLED",        "1")
os.environ.setdefault("SMART_STRIKE_OTM2_CONFIDENCE",     "0.65")
os.environ.setdefault("SMART_STRIKE_OTM3_ENABLED",        "1")
os.environ.setdefault("SMART_STRIKE_OTM3_CONFIDENCE",     "0.75")
os.environ.setdefault("SMART_STRIKE_OTM3_REGIMES",        "BREAKOUT,TRENDING")
os.environ.setdefault("SMART_STRIKE_OTM4_ENABLED",        "1")
os.environ.setdefault("SMART_STRIKE_OTM4_CONFIDENCE",     "0.85")
os.environ.setdefault("SMART_STRIKE_OTM4_REGIMES",        "BREAKOUT")
os.environ.setdefault("STRATEGY_REDIS_PUBLISH_ENABLED",   "0")  # no Redis for sim
os.environ.setdefault("STRATEGY_MIN_CONFIDENCE",          "0.50")
os.environ.setdefault("STRATEGY_PROFILE_ID",              "trader_master_ml_entry_consensus_v1")
# FORCE (not setdefault) — inside the live container STRATEGY_RUN_DIR is already
# set to the live path; setdefault would no-op and the sim would write trades into
# the LIVE positions.jsonl. Always redirect sim output to /tmp.
os.environ["STRATEGY_RUN_DIR"] = "/tmp/sim_run_today"
os.environ.setdefault("MARKET_SESSION_ENABLED",           "0")   # no session gates
os.environ.setdefault("STRATEGY_STARTUP_WARMUP_EVENTS",   "0")
os.environ.setdefault("REDIS_HOST",                       "localhost")
os.environ.setdefault("DEPTH_FEED_ENABLED",               "0")
os.environ.setdefault("BRAIN_ENABLED",                    "false")
os.environ.setdefault("DIRECTION_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib")
os.environ.setdefault("ENTRY_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib")
os.environ.setdefault("ENTRY_ML_MIN_PROB",                "0.65")
os.environ.setdefault("DIRECTION_ML_WEIGHT",              "0.40")
os.environ.setdefault("OPTION_PNL_MODEL_BUNDLE",
    "/app/ml_pipeline_2/artifacts/option_pnl_bundles/option_pnl_atm_pe_9_20260518_063221/option_pnl_atm_pe_9_20260518_063304,"
    "/app/ml_pipeline_2/artifacts/option_pnl_bundles/option_pnl_atm_ce_9_20260518_063305/option_pnl_atm_ce_9_20260518_063335")
# ─────────────────────────────────────────────────────────────────────────────

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
# Suppress noisy module-level loggers
for noisy in ["strategy_app", "contracts_app"]:
    logging.getLogger(noisy).setLevel(logging.ERROR)

REPO = Path("/app")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TODAY = date.today().isoformat()

from strategy_app.engines import DeterministicRuleEngine
from strategy_app.engines.profiles import build_run_metadata
from strategy_app.contracts import SignalType

Path("/tmp/sim_run_today").mkdir(exist_ok=True)

# ── Load engine ───────────────────────────────────────────────────────────────
print("Loading engine with current live config...")
try:
    engine = DeterministicRuleEngine(
        min_confidence=0.50,
        strategy_profile_id="trader_master_ml_entry_consensus_v1",
    )
    from strategy_app.position.exit_policy import build_default_exit_stack
    print("Exit stack:", build_default_exit_stack().name)
    # Apply profile config — same as main.py does at startup
    PROFILE = "trader_master_ml_entry_consensus_v1"
    run_meta = build_run_metadata(PROFILE)
    run_meta["risk_config"] = {
        "rollout_stage": "paper",
        "position_size_multiplier": 1.0,
        "halt_consecutive_losses": 3,
        "halt_daily_dd_pct": 0.04,
    }
    engine.set_run_context(f"sim-{TODAY}", run_meta)
    print("Profile applied:", PROFILE)
except Exception as e:
    print(f"Engine load failed: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# ── Read today's snapshots ───────────────────────────────────────────────────
events_path = REPO / ".run/snapshot_app/events.jsonl"

snapshots = []
for line in events_path.read_text().splitlines():
    try:
        d = json.loads(line)
        snap = d.get("snapshot", d)
        if str(snap.get("trade_date", "")).startswith(TODAY):
            snapshots.append(snap)
    except Exception:
        pass

print(f"Loaded {len(snapshots)} snapshots for {TODAY}")
if not snapshots:
    print("No snapshots found for today")
    sys.exit(1)

# ── Replay ────────────────────────────────────────────────────────────────────
trade_date = date.fromisoformat(TODAY)
engine.on_session_start(trade_date)

trades = []
current_entry = None

print(f"\nReplaying {len(snapshots)} snapshots...\n")

# show first 3 outcomes for debug
_debug_shown = 0

for snap in snapshots:
    try:
        signal = engine.evaluate(snap)
    except Exception as e:
        if _debug_shown < 3:
            print(f"  EVAL ERROR {snap.get('snapshot_id','?')}: {e}")
            _debug_shown += 1
        continue

    if _debug_shown < 3:
        sid = snap.get('snapshot_id','?')
        print(f"  snap {sid}  signal={signal.signal_type.value if signal else None}")
        _debug_shown += 1

    if signal is None:
        continue

    ts = str(snap.get("timestamp", ""))
    hhmm = ts[11:16] if len(ts) > 15 else "?"

    if signal.signal_type == SignalType.ENTRY:
        # Print BANKNIFTY level + OTM chain at entry time
        fut = snap.get("futures_bar", {})
        fut_close = fut.get("close") or fut.get("c")
        # Show chain around ATM
        strikes_data = snap.get("strikes", {})
        atm_approx = round(float(fut_close or 0) / 100) * 100 if fut_close else None
        if atm_approx and len(strikes_data) > 0:
            otm_preview = []
            for step in range(0, 6):
                s = atm_approx - step * 100
                sd = strikes_data.get(str(s)) or strikes_data.get(s)
                if sd:
                    pe_ltp = sd.get("pe_ltp") or sd.get("pe", {}).get("ltp")
                    if pe_ltp:
                        otm_preview.append("  PE %d=%d" % (s, float(pe_ltp)))
            print("  BNFY=%.0f  atm=%d  %s" % (float(fut_close), atm_approx, " | ".join(otm_preview[:5])))
        current_entry = {
            "time_in": hhmm,
            "direction": signal.direction,
            "strike": signal.strike,
            "premium": signal.entry_premium,
            "lots": signal.max_lots,
            "signal_id": signal.signal_id,
        }

    elif signal.signal_type == SignalType.EXIT and current_entry is not None:
        exit_reason = signal.exit_reason.value if signal.exit_reason else "?"
        exit_policy = (signal.decision_metrics or {}).get("exit_policy_triggered", "")
        label = exit_policy if exit_policy else exit_reason

        # Real P&L is in the tracker's closed_positions list (last entry)
        closed = engine._tracker._closed_positions
        if closed:
            cp = closed[-1]
            pnl_pct  = float(cp.get("pnl_pct", 0))
            mfe_pct  = float(cp.get("mfe_pct", 0))
            mae_pct  = float(cp.get("mae_pct", 0))
            exit_prem = float(cp.get("exit_premium", current_entry["premium"]))
        else:
            pnl_pct = mfe_pct = mae_pct = 0.0
            exit_prem = float(current_entry["premium"])

        trades.append({
            "time_in":   current_entry["time_in"],
            "time_out":  hhmm,
            "dir":       current_entry["direction"],
            "strike":    current_entry["strike"],
            "prem_in":   float(current_entry["premium"] or 0),
            "prem_out":  exit_prem,
            "pnl_pct":   pnl_pct,
            "mfe_pct":   mfe_pct,
            "mae_pct":   mae_pct,
            "lots":      current_entry["lots"],
            "exit":      label,
        })
        current_entry = None

engine.on_session_end(trade_date)

# ── Results ───────────────────────────────────────────────────────────────────
print("=" * 105)
print("  %2s  %5s  %5s  %2s  %6s  %8s  %8s  %7s  %7s  %7s  %s" % (
    '#','IN','OUT','D','STRIKE','PREM_IN','PREM_OUT','P&L%','MFE%','MAE%','EXIT'))
print("-" * 105)
for i, t in enumerate(trades, 1):
    sign = "+" if t["pnl_pct"] >= 0 else ""
    print("  %2d  %5s  %5s  %2s  %6s  %8.1f  %8.1f  %s%6.2f%%  %6.2f%%  %6.2f%%  %s" % (
        i, t['time_in'], t['time_out'], t['dir'],
        str(t['strike'] or '?'),
        t['prem_in'], t['prem_out'],
        sign, t['pnl_pct']*100, t['mfe_pct']*100, t['mae_pct']*100,
        t['exit']))

print("=" * 105)

if trades:
    pnls  = [t["pnl_pct"] for t in trades]
    mfes  = [t["mfe_pct"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    total = sum(pnls)
    avg_prem = sum(t["prem_in"] for t in trades) / len(trades)
    avg_mfe  = sum(mfes) / len(mfes)
    # Aggregate capture Σpnl/Σmfe — not mean-of-ratios, which a single
    # small-MFE loser distorts into a nonsense negative figure.
    cap_num = sum(p for p, m in zip(pnls, mfes) if m > 0)
    cap_den = sum(m for m in mfes if m > 0)
    avg_cap = cap_num / cap_den if cap_den > 0 else 0
    print("")
    print("  Trades: %d  |  Wins: %d/%d (%d%%)" % (len(trades), len(wins), len(trades), len(wins)*100//len(trades)))
    print("  Session P&L:  %+.2f%%" % (total*100))
    print("  Avg MFE:      %+.2f%%" % (avg_mfe*100))
    print("  Capture ratio: %.0f%%" % (avg_cap*100))
    print("  Avg entry premium: %.0f INR" % avg_prem)
    print("  Exit reasons:", sorted(set(t['exit'] for t in trades)))
else:
    print("\nNo trades fired in sim")
