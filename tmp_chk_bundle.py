import joblib
path = "ml_pipeline_2/artifacts/research/entry_s1_15m_060pct_hpo_v1_20260609_172649/stages/stage1/model.joblib"
b = joblib.load(path)
print("type:", type(b).__name__)
if isinstance(b, dict):
    print("keys:", sorted(b.keys()))
    print("kind:", b.get("kind"))
    print("features:", b.get("features", [])[:10])
else:
    print("not a dict")
