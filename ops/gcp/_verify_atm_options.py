"""Verify ATM CE/PE options exist for May 28 expiry."""
import json
from ingestion_app.kite_client import create_kite_client

c = json.load(open("ingestion_app/credentials.json"))
kite = create_kite_client(api_key=c["api_key"], access_token=c["access_token"])

symbols = [
    "NFO:BANKNIFTY26MAY55700CE", "NFO:BANKNIFTY26MAY55700PE",
    "NFO:BANKNIFTY26JUN55700CE", "NFO:BANKNIFTY26JUN55700PE",
]
try:
    q = kite.quote(symbols)
    for s, d in q.items():
        print(f"{s}  LTP={d.get('last_price', 0):.2f}  OI={d.get('oi', 0):,}  bid={d.get('depth', {}).get('buy', [{}])[0].get('price', 0):.2f}  ask={d.get('depth', {}).get('sell', [{}])[0].get('price', 0):.2f}")
except Exception as e:
    print(f"Error: {e}")
