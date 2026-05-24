import json, sys
from collections import defaultdict

with open('/tmp/oos_entry_moves.json') as f:
    data = json.load(f)
rows = data['rows']

buckets = [
    ('sub_0.55',  0.0,  0.55),
    ('0.55-0.60', 0.55, 0.60),
    ('0.60-0.65', 0.60, 0.65),
    ('0.65-0.70', 0.65, 0.70),
    ('0.70+',     0.70, 1.01),
]

print(f"{'prob_range':<12} {'n':>3} {'avg_fav':>8} {'med_fav':>8} {'avg_any':>8} {'dir_ok%':>8} {'avg_pnl%':>9} {'orc%':>6}")
for label, lo, hi in buckets:
    sub = [r for r in rows if r.get('entry_prob') is not None and lo <= r['entry_prob'] < hi]
    if not sub:
        continue
    favs = [r['fav_pts_5m'] for r in sub if r.get('fav_pts_5m') is not None]
    anys = [r['max_any_pts_5m'] for r in sub if r.get('max_any_pts_5m') is not None]
    pnls = [r['pnl_pct'] for r in sub]
    ok = [r for r in sub if r.get('det_dir_match') == 'Y']
    orc = [r for r in sub if r.get('oracle_hit_100')]
    med_fav = sorted(favs)[len(favs)//2] if favs else 0
    avg_fav = sum(favs)/len(favs) if favs else 0
    avg_any = sum(anys)/len(anys) if anys else 0
    avg_pnl = sum(pnls)/len(pnls) if pnls else 0
    dir_ok = 100*len(ok)/len(sub)
    orc_pct = 100*len(orc)/len(sub)
    print(f"{label:<12} {len(sub):>3} {avg_fav:>8.1f} {med_fav:>8.0f} {avg_any:>8.1f} {dir_ok:>8.1f} {avg_pnl:>9.2f} {orc_pct:>6.1f}")

print()
print("--- Theoretical ceiling: if direction always correct (avg either-side move) ---")
for label, lo, hi in buckets:
    sub = [r for r in rows if r.get('entry_prob') is not None and lo <= r['entry_prob'] < hi]
    if not sub:
        continue
    anys = [r['max_any_pts_5m'] for r in sub if r.get('max_any_pts_5m') is not None]
    pct60  = 100*sum(1 for x in anys if x >= 60)/len(anys) if anys else 0
    pct100 = 100*sum(1 for x in anys if x >= 100)/len(anys) if anys else 0
    avg_a  = sum(anys)/len(anys) if anys else 0
    print(f"  {label:<12} n={len(sub):>2}  avg_either={avg_a:>6.1f}pt  >=60pt={pct60:>5.1f}%  >=100pt={pct100:>5.1f}%")

print()
print("--- Direction edge: fav_pts > adv_pts means we were on right side ---")
for label, lo, hi in buckets:
    sub = [r for r in rows if r.get('entry_prob') is not None and lo <= r['entry_prob'] < hi
           and r.get('fav_pts_5m') is not None and r.get('adv_pts_5m') is not None]
    if not sub:
        continue
    right = sum(1 for r in sub if r['fav_pts_5m'] > r['adv_pts_5m'])
    wrong = sum(1 for r in sub if r['fav_pts_5m'] < r['adv_pts_5m'])
    tied  = len(sub) - right - wrong
    print(f"  {label:<12} n={len(sub):>2}  right={right}({100*right/len(sub):.0f}%)  wrong={wrong}({100*wrong/len(sub):.0f}%)  tied={tied}")

# win rate and avg PnL by prob band
print()
print("--- Win/Loss by prob band ---")
for label, lo, hi in buckets:
    sub = [r for r in rows if r.get('entry_prob') is not None and lo <= r['entry_prob'] < hi]
    if not sub:
        continue
    wins = [r for r in sub if r['pnl_pct'] > 0]
    avg_win  = sum(r['pnl_pct'] for r in wins)/len(wins) if wins else 0
    losses   = [r for r in sub if r['pnl_pct'] <= 0]
    avg_loss = sum(r['pnl_pct'] for r in losses)/len(losses) if losses else 0
    print(f"  {label:<12} n={len(sub):>2}  win={len(wins)}({100*len(wins)/len(sub):.0f}%)  avg_win={avg_win:>7.1f}%  avg_loss={avg_loss:>7.1f}%")
