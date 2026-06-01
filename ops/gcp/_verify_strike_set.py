"""Verify all 10 strikes (ATM-2 .. ATM+2, MAY) return live quotes."""
import json
from ingestion_app.kite_client import create_kite_client

c = json.load(open("ingestion_app/credentials.json"))
kite = create_kite_client(api_key=c["api_key"], access_token=c["access_token"])

strikes = [55300, 55400, 55500, 55600, 55700]
symbols = []
for s in strikes:
    symbols.append(f"NFO:BANKNIFTY26JUN{s}CE")
    symbols.append(f"NFO:BANKNIFTY26JUN{s}PE")

print(f"requesting {len(symbols)} symbols in one batch...")
try:
    q = kite.quote(symbols)
    print(f"got {len(q)} responses\n")
    for sym in symbols:
        d = q.get(sym, {})
        ltp = d.get("last_price", 0)
        oi = d.get("oi", 0)
        depth = d.get("depth", {}) or {}
        buy = (depth.get("buy") or [])
        sell = (depth.get("sell") or [])
        b1 = (buy[0] or {}).get("price", 0) if buy else 0
        a1 = (sell[0] or {}).get("price", 0) if sell else 0
        print(f"{sym}  LTP={ltp:>8.2f}  OI={oi:>10,}  best_bid={b1:>7.2f}  best_ask={a1:>7.2f}  levels(buy/sell)={len(buy)}/{len(sell)}")
except Exception as e:
    print(f"ERROR: {e}")
