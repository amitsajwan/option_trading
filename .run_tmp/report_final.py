import json
from collections import Counter

RUN_DIR = "/opt/option_trading/.run/strategy_app_sim/6471248e-3edf-4b58-bc77-bebc1260fd98"

blockers = Counter()
signals = 0
for line in open(f"{RUN_DIR}/decisions.jsonl"):
    d = json.loads(line)
    if d.get("action") == "blocked":
        blockers[d.get("blocking_gate", "unknown")] += 1
    elif d.get("action") == "signal":
        signals += 1

signal_count = 0
entry_signals = 0
for line in open(f"{RUN_DIR}/signals.jsonl"):
    d = json.loads(line)
    signal_count += 1
    if d.get("signal_type") == "ENTRY":
        entry_signals += 1

positions = 0
for line in open(f"{RUN_DIR}/positions.jsonl"):
    positions += 1

votes = 0
for line in open(f"{RUN_DIR}/votes.jsonl"):
    votes += 1

closed = []
for line in open(f"{RUN_DIR}/positions.jsonl"):
    d = json.loads(line)
    if d.get("event") == "POSITION_CLOSE":
        closed.append(d)

print("=" * 60)
print("FINAL SIM REPORT: 2026-06-02 (Early MFE Gate: thesis_fail at -3%)")
print("=" * 60)
print(f"Total bars: 375")
print(f"Signals (decisions): {signals}")
print(f"Signals in signals.jsonl: {signal_count}")
print(f"Entry signals: {entry_signals}")
print(f"Positions: {positions}")
print(f"Votes: {votes}")
print(f"Blocked decisions: {sum(blockers.values())}")
print()
print("Blocker distribution:")
for b, c in blockers.most_common():
    pct = c / 375 * 100
    print(f"  {b}: {c} ({pct:.1f}%)")
print()
print("Key findings:")
has_sideways = "sideways_returns_mixed" in blockers
print(f"  sideways_returns_mixed blocker present: {has_sideways}")
if not has_sideways:
    print("  -> Gate fix CONFIRMED: sideways gate is DISABLED")
print(f"  -> Trade signals produced: {entry_signals}")
print()
if closed:
    print(f"Closed positions: {len(closed)}")
    total_pnl = 0
    wins = 0
    losses = 0
    thesis_exits = 0
    for p in closed:
        pnl = p.get("pnl_pct", 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        if p.get("exit_reason") == "THESIS_FAIL":
            thesis_exits += 1
        print(f"  {p['direction']} {p['strike']} | {p['entry_time'][11:16]} -> {p['timestamp'][11:16]} | PnL: {pnl*100:+.2f}% | Exit: {p['exit_reason']} | MFE: {p.get('mfe_pct',0)*100:.2f}% MAE: {p.get('mae_pct',0)*100:.2f}%")
    print()
    print("=" * 60)
    print(f"Total PnL: {total_pnl*100:+.2f}%")
    print(f"Win rate: {wins}/{len(closed)} ({wins/len(closed)*100:.1f}%)")
    print(f"Avg win: {sum(p['pnl_pct'] for p in closed if p['pnl_pct']>0)/max(wins,1)*100:.2f}%")
    print(f"Avg loss: {sum(p['pnl_pct'] for p in closed if p['pnl_pct']<=0)/max(losses,1)*100:.2f}%")
    print(f"THESIS_FAIL exits: {thesis_exits}")
