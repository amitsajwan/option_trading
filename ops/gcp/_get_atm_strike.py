"""Quick ATM-strike probe: read credentials.json, fetch spot, print ATM."""
import json
import sys
from ingestion_app.kite_client import create_kite_client

c = json.load(open("ingestion_app/credentials.json"))
kite = create_kite_client(api_key=c["api_key"], access_token=c["access_token"])

# Try June futures first, fall back to next-available BankNifty future
symbols = ["NFO:BANKNIFTY26JUNFUT", "NFO:BANKNIFTY26JULFUT"]
q = kite.quote(symbols)

for sym, data in q.items():
    ltp = data.get("last_price", 0)
    oi = data.get("oi", 0)
    atm = round(ltp / 100) * 100
    print(f"{sym}  LTP={ltp:.2f}  OI={oi:,}  ATM={atm}")
