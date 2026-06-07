"""Verify the OTM-depth fix on real data — does the new config actually pick deeper/cheaper
strikes than the regressed MAX_OTM_STEPS=1?

Runs the LIVE smart-strike selector (strategy_app/signals/option_selector.select_strike) over
real snapshots from mongo, twice: OLD (MAX_OTM_STEPS=1) vs NEW (MAX_OTM_STEPS=12 + band all-depths),
and reports the OTM-step distribution + premium chosen. No live market needed.

RUN: docker compose run --rm --no-deps -v .../ops:/app/ops --entrypoint sh strategy_app \
       -c 'pip install pymongo -q; python ops/research/verify_otm_selection.py'
"""
from __future__ import annotations

import os
from collections import Counter
from types import SimpleNamespace

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.signals.option_selector import select_strike


def _load(day_limit: int = 3, bar_stride: int = 5):
    from pymongo import MongoClient
    host = os.getenv("MONGO_HOST", "mongo")
    db = MongoClient(f"mongodb://{host}:27017")[os.getenv("MONGO_DB", "trading_ai")]
    coll = db[os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")]
    days = sorted(str(d) for d in coll.distinct("trade_date_ist") if d)[-day_limit:]
    snaps = []
    for day in days:
        for i, d in enumerate(coll.find({"trade_date_ist": day}).sort("timestamp", 1)):
            if i % bar_stride:
                continue
            s = (d.get("payload") or {}).get("snapshot") or {}
            if s.get("strikes"):
                snaps.append(s)
    return days, snaps


def _run(snaps, label: str, max_otm: str) -> None:
    os.environ["STRATEGY_SMART_STRIKE_ENABLED"] = "1"
    os.environ["STRATEGY_STRIKE_SELECTION_POLICY"] = "smart_strike"
    os.environ["STRATEGY_STRIKE_MAX_OTM_STEPS"] = max_otm
    os.environ["SMART_STRIKE_MIN_PREMIUM"] = "600"
    os.environ["SMART_STRIKE_MAX_PREMIUM"] = "1300"
    decision = SimpleNamespace(ce_prob=0.62, pe_prob=0.38)   # confident enough to pass base gate

    steps = Counter()
    premiums = []
    skips = 0
    for s in snaps:
        snap = SnapshotAccessor(s)
        if not snap.atm_strike:
            continue
        sel = select_strike(snap, "CE", decision, regime="")
        if sel.strike is None:
            skips += 1
            continue
        steps[sel.otm_steps] += 1
        p = snap.option_ltp("CE", sel.strike)
        if p:
            premiums.append(float(p))
    n = sum(steps.values())
    avg = sum(premiums) / len(premiums) if premiums else 0.0
    dist = ", ".join(f"{k}OTM:{v}" for k, v in sorted(steps.items()))
    print(f"\n{label}: selected={n} skip(no-affordable)={skips}")
    print(f"  OTM-step distribution: {dist}")
    print(f"  avg selected premium: Rs{avg:.0f}   (deeper OTM = cheaper)")


def main() -> None:
    days, snaps = _load()
    print(f"days={days} sampled_bars={len(snaps)}")
    _run(snaps, "OLD (MAX_OTM_STEPS=1 — the regression)", "1")
    _run(snaps, "NEW (MAX_OTM_STEPS=12 + band all-depths)", "12")
    print("\nIf NEW shifts mass to deeper OTM at lower premium, the fix has effect.")


if __name__ == "__main__":
    main()
