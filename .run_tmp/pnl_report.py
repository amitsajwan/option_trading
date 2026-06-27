import json

RUN_DIR = "/opt/option_trading/.run/strategy_app_sim/ac436e95-22b7-4521-b975-9b974f14a9d2"

closed = []
for line in open(f"{RUN_DIR}/positions.jsonl"):
    d = json.loads(line)
    if d.get("event") == "POSITION_CLOSE":
        closed.append(d)

if not closed:
    print("No closed positions found")
else:
    print(f"Closed positions: {len(closed)}")
    print()
    total_pnl = 0
    wins = 0
    losses = 0
    for p in closed:
        pnl = p.get("pnl_pct", 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        print(f"  {p['direction']} {p['strike']} | {p['entry_time'][11:16]} -> {p['timestamp'][11:16]} | "
              f"P&L: {pnl*100:+.2f}% | Exit: {p['exit_reason']} | "
              f"MFE: {p.get('mfe_pct',0)*100:.2f}% MAE: {p.get('mae_pct',0)*100:.2f}%")

    print()
    print("=" * 50)
    print(f"Total P&L: {total_pnl*100:+.2f}%")
    print(f"Win rate: {wins}/{len(closed)} ({wins/len(closed)*100:.1f}%)")
    print(f"Avg win: {sum(p['pnl_pct'] for p in closed if p['pnl_pct']>0)/max(wins,1)*100:.2f}%")
    print(f"Avg loss: {sum(p['pnl_pct'] for p in closed if p['pnl_pct']<=0)/max(losses,1)*100:.2f}%")
