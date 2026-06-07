"""Probe: how complete is per-strike intrabar OHLC in phase1_market_snapshots?

Exit fidelity depends on per-strike ce_high/ce_low/pe_high/pe_low being populated. If they
are, the 1-min option bar already gives intrabar exit precision; if not, we'd need ticks.
"""
from __future__ import annotations

import math
import os

from pymongo import MongoClient


def _fin(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def main() -> None:
    host = os.getenv("MONGO_HOST", "mongo")
    db = MongoClient(f"mongodb://{host}:27017")[os.getenv("MONGO_DB", "trading_ai")]
    coll = db[os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")]
    days = sorted(str(d) for d in coll.distinct("trade_date_ist") if d)

    rows = ce_ltp = ce_hl = pe_ltp = pe_hl = 0
    sample = None
    for day in days:
        for d in coll.find({"trade_date_ist": day}).limit(200):
            s = (d.get("payload") or {}).get("snapshot") or {}
            for r in (s.get("strikes") or []):
                rows += 1
                if _fin(r.get("ce_ltp")):
                    ce_ltp += 1
                if _fin(r.get("ce_high")) and _fin(r.get("ce_low")):
                    ce_hl += 1
                if _fin(r.get("pe_ltp")):
                    pe_ltp += 1
                if _fin(r.get("pe_high")) and _fin(r.get("pe_low")):
                    pe_hl += 1
                if sample is None and _fin(r.get("ce_high")):
                    sample = {k: r.get(k) for k in ("strike", "ce_ltp", "ce_open", "ce_high", "ce_low",
                                                    "pe_ltp", "pe_high", "pe_low")}

    print(f"days={len(days)} strike_rows_scanned={rows} (200 docs/day cap)")
    if rows:
        print(f"  ce_ltp present : {ce_ltp / rows:.1%}")
        print(f"  ce_high+low    : {ce_hl / rows:.1%}")
        print(f"  pe_ltp present : {pe_ltp / rows:.1%}")
        print(f"  pe_high+low    : {pe_hl / rows:.1%}")
    print(f"  sample row w/ high: {sample}")


if __name__ == "__main__":
    main()
