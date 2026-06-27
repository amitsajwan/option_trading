"""Update .env.compose to activate ML_ENTRY with velocity_base bundle at threshold=0.049."""
import re, shutil

f = "/opt/option_trading/.env.compose"
shutil.copy(f, f + ".bak_pre_velocitybase")
txt = open(f).read()

changes = {
    "ENTRY_ML_MODEL_PATH": "/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib",
    "ENTRY_ML_MIN_PROB":   "0.049",
    "ENTRY_VOL_GATE_ENABLED": "0",
}

for key, val in changes.items():
    pattern = rf"^({re.escape(key)}=).*$"
    replacement = rf"\g<1>{val}"
    new_txt, n = re.subn(pattern, replacement, txt, flags=re.MULTILINE)
    if n == 0:
        print(f"WARNING: {key} not found in file, appending")
        txt = txt.rstrip() + f"\n{key}={val}\n"
    else:
        print(f"Updated {key} -> {val}")
        txt = new_txt

open(f, "w").write(txt)

# Verify
for key in changes:
    m = re.search(rf"^{re.escape(key)}=(.*)$", txt, re.MULTILINE)
    print(f"  VERIFY {key}: {m.group(1) if m else 'NOT FOUND'}")
print("Done. Restart the container to apply.")
