"""Check bundle kind tags and probability scale of research bundles vs live v3."""
import sys, os, joblib
sys.path.insert(0, "/home/amits/bmm_run/ml_pipeline_2/src")
sys.path.insert(0, "/home/amits/bmm_run")
from pathlib import Path

ARTIFACTS  = Path("/home/amits/bmm_run/ml_pipeline_2/artifacts/research")
LIVE_MODEL = os.path.expandvars("${ENTRY_ML_MODEL_PATH}")

def inspect(name, path):
    b = joblib.load(path)
    if not isinstance(b, dict):
        print(f"{name}: raw estimator (no dict wrapper)")
        return
    keys = list(b.keys())
    kind = b.get("kind") or b.get("bundle_kind") or b.get("_kind") or "NOT FOUND"
    feats = b.get("feature_columns") or (b.get("_model_input_contract") or {}).get("feature_columns") or []
    holdout = (b.get("holdout_eval") or {}).get("roc_auc")
    models = b.get("models")
    est = None
    if isinstance(models, dict):
        est = models.get("move") or next(iter(models.values()), None)
    else:
        est = models
    est_type = type(est).__name__ if est else "None"
    print(f"\n  [{name}]")
    print(f"    path:        {path}")
    print(f"    top-level keys: {keys}")
    print(f"    kind:        {kind}")
    print(f"    features:    {len(feats)}")
    print(f"    holdout_auc: {holdout}")
    print(f"    estimator:   {est_type}")
    # check calibration wrapper
    if est is not None:
        print(f"    est.__class__: {est.__class__.__module__}.{est.__class__.__name__}")

bundles = [
    ("velocity_base", sorted(ARTIFACTS.glob("ab_5m020_base_*/stages/stage1/model.joblib"))),
    ("bmm_prod",      sorted(ARTIFACTS.glob("bmm_prod_5m020_v2view_*/stages/stage1/model.joblib"))),
]
for name, paths in bundles:
    if paths:
        inspect(name, paths[0])
    else:
        print(f"\n  [{name}] NOT FOUND")

# also check if live entry_only_v3 bundle exists anywhere
print("\n=== Searching for entry_only_v3 bundles ===")
live_paths = sorted(Path("/home/amits").rglob("*entry_only*model.joblib"))
for p in live_paths[:5]:
    print(f"  {p}")
if not live_paths:
    print("  none found under /home/amits")
