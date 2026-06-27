import sys, joblib
sys.path.insert(0, "/home/amits/bmm_run/ml_pipeline_2/src")
sys.path.insert(0, "/home/amits/bmm_run")
from pathlib import Path

ARTIFACTS = Path("/home/amits/bmm_run/ml_pipeline_2/artifacts/research")

def get_features(glob_pat):
    paths = sorted(ARTIFACTS.glob(glob_pat))
    if not paths:
        return None, None
    b = joblib.load(paths[0] / "stages/stage1/model.joblib")
    if isinstance(b, dict):
        feats = b.get("feature_columns")
        if feats is None:
            c = b.get("_model_input_contract") or {}
            feats = c.get("feature_columns") or c.get("features")
    return list(feats) if feats else None

vel = get_features("ab_5m020_base_*")
bmm = get_features("bmm_prod_5m020_v2view_*")

vel_set = set(vel)
bmm_set = set(bmm)

print(f"velocity_base: {len(vel)} features")
print(f"bmm_prod:      {len(bmm)} features")
print()

# Compression-related feature names
comp_keywords = ["compression", "bb_width", "range_ratio", "candle_overlap", "bb_pct", "atr_ratio",
                 "stored_energy", "ema_spread", "ema_order", "position_in_day"]

print("=== velocity_base features by category ===")
vel_comp = [f for f in vel if any(k in f for k in comp_keywords)]
vel_vel  = [f for f in vel if f.startswith("vel_") or f.startswith("ctx_")]
vel_other = [f for f in vel if f not in vel_comp and f not in vel_vel]
print(f"compression-like ({len(vel_comp)}): {vel_comp}")
print(f"velocity/ctx    ({len(vel_vel)}): {vel_vel[:10]}...")
print(f"other           ({len(vel_other)}): {vel_other}")

print()
print("=== bmm_prod EXTRA features (in bmm but not velocity_base) ===")
extra = sorted(bmm_set - vel_set)
print(f"Extra ({len(extra)}): {extra}")

print()
print("=== velocity_base features NOT in bmm_prod ===")
vel_only = sorted(vel_set - bmm_set)
print(f"vel-only ({len(vel_only)}): {vel_only}")
