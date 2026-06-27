import json, shutil
f = "/opt/option_trading/.run/strategy_app/ops_env.json"
shutil.copy(f, f + ".bak_pre_velocitybase")
d = json.load(open(f))
print("OLD ENTRY_ML_MODEL_PATH:", d["ENTRY_ML_MODEL_PATH"])
print("OLD ENTRY_ML_MIN_PROB:  ", d["ENTRY_ML_MIN_PROB"])
d["ENTRY_ML_MODEL_PATH"] = "/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib"
d["ENTRY_ML_MIN_PROB"] = "0.049"
open(f, "w").write(json.dumps(d, indent=2))
d2 = json.load(open(f))
print("NEW ENTRY_ML_MODEL_PATH:", d2["ENTRY_ML_MODEL_PATH"])
print("NEW ENTRY_ML_MIN_PROB:  ", d2["ENTRY_ML_MIN_PROB"])
print("Done.")
