#!/usr/bin/env python3
"""Diagnose which ML features are NaN at runtime for a SIM snapshot."""
import json
import os
import sys

sys.path.insert(0, "/opt/option_trading")
sys.path.insert(0, "/app")

from pathlib import Path
import joblib

# Load the entry model bundle from the container env
bundle_path = os.getenv("ENTRY_ML_MODEL_PATH", "/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib")

print("bundle_path:", bundle_path)
if not bundle_path:
    sys.exit(1)

bundle = joblib.load(bundle_path)
features = list(bundle.get("features") or [])
print("features:", len(features))

# Load a snapshot from the SIM run
snap_path = "/opt/option_trading/.run/strategy_app_sim/e4509096-6268-483d-ae19-fe0525397852/snapshots.jsonl"
if not Path(snap_path).exists():
    # Try loading from the phase1_market_snapshots collection via a dump
    print("snapshots.jsonl not found, use Mongo export instead")
    sys.exit(1)

with open(snap_path) as f:
    lines = f.readlines()

# Pick the first bar where the signal fired (09:48)
target_time = "2026-06-18T09:48:00"
raw = None
for line in lines:
    d = json.loads(line)
    if target_time in d.get("timestamp", ""):
        raw = d.get("payload", d)
        break

if raw is None:
    raw = json.loads(lines[0]).get("payload", json.loads(lines[0]))

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

snap = SnapshotAccessor(raw)
vel = snap.velocity_features
if isinstance(vel, dict):
    flat.update(vel)

nan_features = []
for f in features:
    val = flat.get(f)
    try:
        fv = float(val) if val is not None else float("nan")
    except (TypeError, ValueError):
        fv = float("nan")
    if fv != fv:  # NaN
        nan_features.append(f)

print(f"NaN features: {len(nan_features)}/{len(features)}")
for f in nan_features[:20]:
    print("  -", f)
if len(nan_features) > 20:
    print(f"  ... and {len(nan_features) - 20} more")
