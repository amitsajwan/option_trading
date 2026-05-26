#!/usr/bin/env python3
"""Sample snapshots and compare entry + unified vs dual direction gates."""
from __future__ import annotations

import os
import sys
from pathlib import Path

for _root in (Path("/app"), Path(__file__).resolve().parent.parent.parent):
    try:
        if (_root / "strategy_app").is_dir() and str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
            break
    except IndexError:
        continue

from pymongo import MongoClient
from strategy_app.engines.strategies.ml_entry import (
    _resolve_direction,
    _resolve_direction_dual,
    _load_dir_bundle,
)
from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.ml.bundle_inference import predict_positive_class_prob

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
DB = os.environ.get("MONGO_DB", "trading_ai")
COLL = os.environ.get("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
ENTRY_PATH = os.environ.get(
    "ENTRY_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib",
)
UNIFIED_PATH = os.environ.get(
    "DIRECTION_UNIFIED_PATH",
    "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib",
)
DUAL_PATH = os.environ.get(
    "DIRECTION_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/direction_dual/published/direction_dual_model.joblib",
)
MIN_PROB = float(os.environ.get("ENTRY_ML_MIN_PROB", "0.65"))
LIMIT = int(os.environ.get("PROBE_LIMIT", "8000"))
DATE_FROM = os.environ.get("PROBE_DATE_FROM", "2024-05-01")
DATE_TO = os.environ.get("PROBE_DATE_TO", "2024-07-31")


def main() -> int:
    import joblib

    entry = joblib.load(ENTRY_PATH)
    unified = joblib.load(UNIFIED_PATH) if Path(UNIFIED_PATH).is_file() else None
    dual = _load_dir_bundle(DUAL_PATH)

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    coll = client[DB][COLL]
    q = {"trade_date_ist": {"$gte": DATE_FROM, "$lte": DATE_TO}}
    cursor = coll.find(q, {"_id": 0}).sort("timestamp", 1).limit(LIMIT)

    n = 0
    debug_probs: list[float] = []
    entry_ok = 0
    unified_ok = 0
    dual_ok = 0
    entry_and_unified = 0
    entry_and_dual = 0
    ce_probs: list[float] = []
    pe_probs: list[float] = []

    for doc in cursor:
        n += 1
        snap = SnapshotAccessor(doc)
        ep = predict_positive_class_prob(entry, snap)
        if ep is not None and len(debug_probs) < 20:
            debug_probs.append(ep)
        if ep is None or ep < MIN_PROB:
            continue
        entry_ok += 1

        if unified and unified.get("kind") == "direction_only_bundle":
            cp = predict_positive_class_prob(unified, snap)
            if cp is not None:
                unified_ok += 1
                entry_and_unified += 1

        if dual:
            ce_sub = dual.get("ce_bundle")
            pe_sub = dual.get("pe_bundle")
            cw = predict_positive_class_prob(ce_sub, snap) if isinstance(ce_sub, dict) else None
            pw = predict_positive_class_prob(pe_sub, snap) if isinstance(pe_sub, dict) else None
            if cw is not None:
                ce_probs.append(cw)
            if pw is not None:
                pe_probs.append(pw)
            if _resolve_direction_dual(dual, snap) is not None:
                dual_ok += 1
                entry_and_dual += 1

    def _pct(x: int, d: int) -> str:
        return f"{100.0 * x / d:.1f}%" if d else "n/a"

    if debug_probs:
        print(f"  sample entry_probs (first {len(debug_probs)}): {[round(p, 3) for p in debug_probs]}")
    elif n:
        print("  sample entry_probs: all None (feature extraction may not match replay payloads)")
    print(f"snapshots_sampled={n} entry_prob>={MIN_PROB}: {entry_ok} ({_pct(entry_ok, n)})")
    print(f"  unified direction ok: {unified_ok}  entry+unified: {entry_and_unified}")
    print(f"  dual direction ok:    {dual_ok}  entry+dual:    {entry_and_dual}")
    if ce_probs:
        ce_probs.sort()
        print(
            f"  ce_win dist: min={ce_probs[0]:.3f} p50={ce_probs[len(ce_probs)//2]:.3f} "
            f"p90={ce_probs[int(len(ce_probs)*0.9)]:.3f} max={ce_probs[-1]:.3f}"
        )
    if pe_probs:
        pe_probs.sort()
        print(
            f"  pe_win dist: min={pe_probs[0]:.3f} p50={pe_probs[len(pe_probs)//2]:.3f} "
            f"p90={pe_probs[int(len(pe_probs)*0.9)]:.3f} max={pe_probs[-1]:.3f}"
        )
    both_below = 0
    if ce_probs and pe_probs and len(ce_probs) == len(pe_probs):
        for c, p in zip(ce_probs, pe_probs):
            if c < 0.5 and p < 0.5:
                both_below += 1
        print(f"  among entry_ok with both probs: both<0.5: {both_below}/{len(ce_probs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
