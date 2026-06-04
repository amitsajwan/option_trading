"""Probe: how deep does the option chain in our snapshots actually go, and what
are the premiums per OTM step? Answers "do we even have ₹200-600 OTM strikes?"."""
import json, sys
from pathlib import Path
REPO = Path("/app")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from strategy_app.market.snapshot_accessor import SnapshotAccessor

date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-02"
events = (REPO / ".run/snapshot_app/events.jsonl").read_text(encoding="utf-8").splitlines()
snaps = []
for l in events:
    try:
        d = json.loads(l); s = d.get("snapshot", d)
        if str(s.get("trade_date", "")).startswith(date):
            snaps.append(s)
    except Exception:
        pass
print(f"{date}: {len(snaps)} snapshots")
# sample 3 bars across the session
for idx in (len(snaps)//4, len(snaps)//2, 3*len(snaps)//4):
    s = snaps[idx]
    acc = SnapshotAccessor(s)
    atm = acc.atm_strike
    step = acc.strike_step() if callable(getattr(acc, "strike_step", None)) else None
    print(f"\n--- {str(s.get('timestamp'))[11:16]}  atm={atm} step={step} ---")
    if not atm or not step:
        continue
    for dirn in ("CE", "PE"):
        deepest = None
        band_hits = []
        last_priced = None
        for n in range(0, 40):
            strike = atm + n * step if dirn == "CE" else atm - n * step
            ltp = acc.option_ltp(dirn, strike)
            if ltp is not None:
                deepest = n
                last_priced = int(ltp)
                if 200 <= ltp <= 600:
                    band_hits.append(f"{n}OTM={int(ltp)}")
        print(f"  {dirn}: deepest priced strike = {deepest} steps OTM "
              f"(premium {last_priced}); strikes in 200-600 band: {band_hits if band_hits else 'NONE'}")
