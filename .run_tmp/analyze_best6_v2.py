import json
from collections import defaultdict

RUN_DIR = "/opt/option_trading/.run/strategy_app_sim/0b347971-fedc-4113-9265-6c12161ec957"

# Load signals with entry info
signals = []
for line in open(f"{RUN_DIR}/signals.jsonl"):
    d = json.loads(line)
    if d.get("signal_type") == "ENTRY":
        signals.append(d)

# Load closed positions
closed = []
for line in open(f"{RUN_DIR}/positions.jsonl"):
    d = json.loads(line)
    if d.get("event") == "POSITION_CLOSE":
        closed.append(d)

print("=" * 70)
print("TRADE ANALYSIS - Finding threshold for best 6 trades")
print("=" * 70)

# Merge signals with their closed positions by signal_id
for s in signals:
    sid = s.get("signal_id")
    pos = next((p for p in closed if p.get("signal_id") == sid), None)
    if pos:
        s["pnl_pct"] = pos.get("pnl_pct", 0)
        s["exit_reason"] = pos.get("exit_reason", "")
        s["mfe_pct"] = pos.get("mfe_pct", 0)
        s["mae_pct"] = pos.get("mae_pct", 0)
        s["bars_held"] = pos.get("bars_held", 0)
    else:
        s["pnl_pct"] = None

# Sort by PnL
ranked = sorted([s for s in signals if s.get("pnl_pct") is not None], key=lambda x: x["pnl_pct"], reverse=True)

print("\nAll 10 trades ranked by PnL:")
print(f"{'Rank':<4} {'Dir':<3} {'Strike':<6} {'PnL':<8} {'Conf':<6} {'Regime':<12} {'Exit':<16} {'MFE':<7} {'MAE':<7}")
print("-" * 70)
for i, s in enumerate(ranked, 1):
    print(f"{i:<4} {s.get('direction',''):<3} {s.get('strike',''):<6} {s.get('pnl_pct',0)*100:>+.2f}%  {s.get('confidence',0):.2f}   {s.get('entry_regime_name','')[:11]:<12} {s.get('exit_reason','')[:15]:<16} {s.get('mfe_pct',0)*100:>+.2f}%  {s.get('mae_pct',0)*100:>+.2f}%")

best6 = ranked[:6]
worst4 = ranked[6:]

best_pnl = sum(s["pnl_pct"] for s in best6) * 100
worst_pnl = sum(s["pnl_pct"] for s in worst4) * 100

print(f"\nBest 6 PnL: {best_pnl:+.2f}%")
print(f"Worst 4 PnL: {worst_pnl:+.2f}%")
print(f"Difference: {best_pnl - worst_pnl:+.2f}%")

print("\n" + "=" * 70)
print("THRESHOLD SIMULATION:")
print("=" * 70)

# Test various thresholds
thresholds = [
    ("confidence >= 0.70", lambda s: s["confidence"] >= 0.70),
    ("confidence >= 0.75", lambda s: s["confidence"] >= 0.75),
    ("regime != BREAKOUT", lambda s: s.get("entry_regime_name") != "BREAKOUT"),
    ("regime == SIDEWAYS", lambda s: s.get("entry_regime_name") == "SIDEWAYS"),
    ("confidence >= 0.70 OR regime == SIDEWAYS", lambda s: s["confidence"] >= 0.70 or s.get("entry_regime_name") == "SIDEWAYS"),
]

for name, fn in thresholds:
    filtered = [s for s in ranked if fn(s)]
    pnl = sum(s["pnl_pct"] for s in filtered) * 100
    wins = sum(1 for s in filtered if s["pnl_pct"] > 0)
    print(f"\n  {name}:")
    print(f"    Trades: {len(filtered)}, PnL: {pnl:+.2f}%, Win rate: {wins}/{len(filtered)} ({wins/max(len(filtered),1)*100:.0f}%)")
    for s in filtered:
        print(f"      {s.get('direction','')} {s.get('strike','')} {s['pnl_pct']*100:>+.2f}% conf={s['confidence']:.2f} {s.get('entry_regime_name','')}")
