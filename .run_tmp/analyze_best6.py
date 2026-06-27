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
print("TRADE ANALYSIS — Finding threshold for best 6 trades")
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

print("\n" + "=" * 70)
print("TOP 6 TRADES (by PnL):")
best6 = ranked[:6]
worst4 = ranked[6:]

best_pnl = sum(s["pnl_pct"] for s in best6) * 100
worst_pnl = sum(s["pnl_pct"] for s in worst4) * 100

print(f"  Best 6 PnL: {best_pnl:+.2f}%")
print(f"  Worst 4 PnL: {worst_pnl:+.2f}%")
print(f"  Difference: {best_pnl - worst_pnl:+.2f}%")

# Analyze what distinguishes best6 from worst4
print("\n" + "=" * 70)
print("DISCRIMINATING FACTORS:")

# Confidence threshold
best_conf = [s["confidence"] for s in best6]
worst_conf = [s["confidence"] for s in worst4]
print(f"\nConfidence:")
print(f"  Best 6: min={min(best_conf):.2f}, max={max(best_conf):.2f}, avg={sum(best_conf)/len(best_conf):.2f}")
print(f"  Worst 4: min={min(worst_conf):.2f}, max={max(worst_conf):.2f}, avg={sum(worst_conf)/len(worst_conf):.2f}")

# Regime distribution
best_regimes = defaultdict(int)
worst_regimes = defaultdict(int)
for s in best6:
    best_regimes[s.get("entry_regime_name","")] += 1
for s in worst4:
    worst_regimes[s.get("entry_regime_name","")] += 1
print(f"\nRegime distribution:")
print(f"  Best 6: {dict(best_regimes)}")
print(f"  Worst 4: {dict(worst_regimes)}")

# MFE vs MAE
best_mfe = [s["mfe_pct"] for s in best6]
best_mae = [s["mae_pct"] for s in best6]
worst_mfe = [s["mfe_pct"] for s in worst4]
worst_mae = [s["mae_pct"] for s in worst4]
print(f"\nMFE (max favorable excursion):")
print(f"  Best 6: avg={sum(best_mfe)/len(best_mfe)*100:.2f}%")
print(f"  Worst 4: avg={sum(worst_mfe)/len(worst_mfe)*100:.2f}%")
print(f"\nMAE (max adverse excursion):")
print(f"  Best 6: avg={sum(best_mae)/len(best_mae)*100:.2f}%")
print(f"  Worst 4: avg={sum(worst_mae)/len(worst_mae)*100:.2f}%")

# Time of day
print(f"\nTime of day:")
for s in best6:
    print(f"  BEST: {s['entry_time'][11:16]} {s.get('direction','')} {s.get('strike','')} PnL={s['pnl_pct']*100:+.2f}%")
for s in worst4:
    print(f"  WORST: {s['entry_time'][11:16]} {s.get('direction','')} {s.get('strike','')} PnL={s['pnl_pct']*100:+.2f}%")

print("\n" + "=" * 70)
print("RECOMMENDED THRESHOLD:")

# Find confidence cutoff that captures best6 but excludes worst4
for thresh in [0.65, 0.70, 0.75, 0.80, 0.85]:
    captured = [s for s in best6 if s["confidence"] >= thresh]
    excluded = [s for s in worst4 if s["confidence"] < thresh]
    filtered_out = [s for s in best6 if s["confidence"] < thresh]
    let_in = [s for s in worst4 if s["confidence"] >= thresh]
    pnl_if_applied = sum(s["pnl_pct"] for s in best6 if s["confidence"] >= thresh) * 100
    print(f"  conf >= {thresh:.2f}: captures {len(captured)}/6 best, excludes {len(excluded)}/4 worst, "
          f"PnL={pnl_if_applied:+.2f}%, filtered out good={len(filtered_out)}, let in bad={len(let_in)}")

# Find regime-based filter
print(f"\n  Regime filter (exclude TRENDING):")
trending_bad = [s for s in worst4 if s.get("entry_regime_name") == "TRENDING"]
non_trending_good = [s for s in best6 if s.get("entry_regime_name") != "TRENDING"]
print(f"    Would exclude {len(trending_bad)} bad trades, keep {len(non_trending_good)} good trades")

print(f"\n  Combined: confidence >= 0.70 AND not TRENDING with STOP_LOSS:")
# This is getting complex, let's just show the raw recommendation
