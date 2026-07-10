"""Training view from quality-gated Mongo snapshots (weekend rebuild, 2026-07-11).

One table for BOTH the label study and training:
  - features: EXACTLY the serving path (strategy_app.ml.bundle_inference.
    build_feature_row on a SnapshotAccessor) — train/serve parity by
    construction, no separate feature code to drift.
  - labels: a MENU of candidate forward outcomes per bar (the label study
    picks; training consumes the chosen one). Computed from the day's own
    futures closes — no lookahead beyond the labeled horizon.
  - provenance: only days with dataset_manifests.passed=true are eligible
    (the ingest quality gate is enforced end-to-end).

Run inside a container with strategy_app + snapshot_app + pymongo + pandas
(seller_app / dashboard images):
  python -m ml_pipeline_2.scripts.build_training_view_from_mongo \
      --instrument BANKNIFTY --from 2024-11-01 --to 2026-05-23 \
      --out /shared_run/training_view
Output: one csv.gz per instrument (+ a manifest json with row counts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd


HORIZONS_MIN = (5, 10, 15, 30)


def _feature_list() -> list[str]:
    """The union of the live entry bundles' feature lists — what serving reads."""
    import joblib
    feats: list[str] = []
    for p in ("/app/models/dhan_entry_bundle_v3.joblib",
              "/app/models/nifty_entry_bundle_v3.joblib",
              "/app/models/dhan_entry_fast_v1.joblib"):
        try:
            b = joblib.load(p)
            for f in b.get("features") or []:
                if f not in feats:
                    feats.append(f)
        except Exception:
            pass
    if not feats:
        raise SystemExit("no model bundles found to derive the feature list")
    return feats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="BANKNIFTY")
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--out", default="/shared_run/training_view")
    args = ap.parse_args()

    from pymongo import MongoClient
    from strategy_app.market.snapshot_accessor import SnapshotAccessor
    from strategy_app.ml.bundle_inference import build_feature_row

    inst = args.instrument.upper()
    coll_name = ("phase1_market_snapshots_hist" if inst == "BANKNIFTY"
                 else f"phase1_market_snapshots_hist_{inst.lower()}")
    db = MongoClient(os.getenv("MONGO_HOST", "mongo"), 27017)[os.getenv("MONGO_DB", "trading_ai")]

    verified_days = {m["trade_date"] for m in db["dataset_manifests"].find(
        {"passed": True, "instrument": inst,
         "trade_date": {"$gte": args.d_from, "$lte": args.d_to}})}
    print(f"{inst}: {len(verified_days)} quality-verified days in range", flush=True)

    features = _feature_list()
    rows: list[dict] = []
    for day in sorted(verified_days):
        day_rows: list[dict] = []
        for doc in db[coll_name].find({"trade_date_ist": day}, sort=[("timestamp", 1)]):
            snap = (doc.get("payload") or {}).get("snapshot")
            if not snap:
                continue
            acc = SnapshotAccessor(snap)
            frow = build_feature_row(acc, features) or {}
            fut = (snap.get("futures_bar") or {}).get("fut_close")
            day_rows.append({
                "trade_date": day,
                "time": (snap.get("session_context") or {}).get("time"),
                "fut_close": fut,
                "dte": (snap.get("session_context") or {}).get("days_to_expiry"),
                "iv_pct": (snap.get("iv_derived") or {}).get("iv_percentile"),
                **frow,
            })
        # label menu from the day's own close series (no cross-day lookahead)
        closes = [r["fut_close"] for r in day_rows]
        n = len(day_rows)
        for i, r in enumerate(day_rows):
            c0 = closes[i]
            for h in HORIZONS_MIN:
                w = [c for c in closes[i + 1:i + 1 + h] if c]
                if not w or not c0:
                    r[f"fwd_maxabs_{h}m"] = None
                    continue
                r[f"fwd_maxabs_{h}m"] = round(max(abs(c - c0) for c in w), 2)
            r["bars_left"] = n - i - 1
        rows.extend(day_rows)
        if len(rows) % 20000 < 400:
            print(f"  ... {day} total_rows={len(rows)}", flush=True)

    df = pd.DataFrame(rows)
    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, f"training_view_{inst.lower()}.csv.gz")
    df.to_csv(out_path, index=False, compression="gzip")
    manifest = {
        "instrument": inst, "rows": len(df), "days": len(verified_days),
        "features": len(features), "horizons": list(HORIZONS_MIN),
        "from": args.d_from, "to": args.d_to,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_collection": coll_name, "quality_gated": True,
    }
    with open(os.path.join(args.out, f"training_view_{inst.lower()}.manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
