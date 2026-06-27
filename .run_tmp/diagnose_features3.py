#!/usr/bin/env python3
"""Diagnose which ML features are NaN at runtime for a dumped snapshot JSON."""
import json
import os
import sys

sys.path.insert(0, "/opt/option_trading")
sys.path.insert(0, "/app")

from pathlib import Path
import joblib

bundle_path = os.getenv(
    "ENTRY_ML_MODEL_PATH",
    "/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib",
)
print("bundle_path:", bundle_path)
bundle = joblib.load(bundle_path)
features = list(bundle.get("features") or [])
print("features:", len(features))

raw_path = "/tmp/snap_0948.json"
with open(raw_path) as f:
    snap_doc = json.load(f)

raw = snap_doc.get("payload", snap_doc)
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

extra = [k for k in flat if k not in features]
print(f"\nextra fields in snapshot not used by model: {len(extra)}")
for k in extra[:20]:
    print("  -", k)
