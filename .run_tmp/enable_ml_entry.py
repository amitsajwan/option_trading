import json
f = "/opt/option_trading/.run/strategy_app/ops_env.json"
d = json.load(open(f))
d["ENTRY_VOL_GATE_ENABLED"] = "0"
open(f, "w").write(json.dumps(d, indent=2))
d2 = json.load(open(f))
print("ENTRY_VOL_GATE_ENABLED:", d2["ENTRY_VOL_GATE_ENABLED"])
print("ENTRY_ML_MODEL_PATH:   ", d2["ENTRY_ML_MODEL_PATH"])
print("ENTRY_ML_MIN_PROB:     ", d2["ENTRY_ML_MIN_PROB"])
print("Done.")
