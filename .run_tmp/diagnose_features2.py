#!/usr/bin/env python3
"""Diagnose which ML features are NaN at runtime for a live snapshot."""
import os
import sys

sys.path.insert(0, "/opt/option_trading")
sys.path.insert(0, "/app")

import math
from pathlib import Path
import joblib
import pymongo

bundle_path = os.getenv(
    "ENTRY_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib",
)
print("bundle_path:", bundle_path)
bundle = joblib.load(bundle_path)
features = list(bundle.get("features") or [])
print("features:", len(features))

client = pymongo.MongoClient("mongodb://mongo:27017/trading_ai")
db = client.trading_ai
snap = db.phase1_market_snapshots.find_one(
    {"trade_date_ist": "2026-06-18", "market_time_ist": "09:48:00"}
)
if not snap:
    print("snapshot not found")
    sys.exit(1)

raw = snap.get("payload", snap)
print("snapshot timestamp:", raw.get("timestamp"), "id:", raw.get("snapshot_id"))

from snapshot_app.core.stage_views import project_stage_views_v2
from strategy_app.market.snapshot_accessor import SnapshotAccessor

views = project_stage_views_v2(raw)
flat = {}
for v in views.values():
    if isinstance(v, dict):
        flat.update(v)
for k, v in raw.items():
    if k not in flat and not isinstance(v, (dict, list)):
        flat[k] = v

snap_acc = SnapshotAccessor(raw)
vel = snap_acc.velocity_features
if isinstance(vel, dict):
    flat.update(vel)

nan_features = []
for f in features:
    val = flat.get(f)
    try:
        fv = float(val) if val is not None else float("nan")
    except (TypeError, ValueError):
        fv = float("nan")
    if fv != fv:
        nan_features.append(f)

print(f"NaN features: {len(nan_features)}/{len(features)}")
for f in nan_features:
    print("  -", f)

# Also check which features are present in flat but not in features
extra = [k for k in flat if k not in features]
print(f"\nextra fields in snapshot not used by model: {len(extra)}")
for k in extra[:20]:
    print("  -", k)
