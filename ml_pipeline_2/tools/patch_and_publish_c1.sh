#!/usr/bin/env bash
# patch_and_publish_c1.sh
# Force-publish C1 research run locally despite hard gate failures.
# Sets smoke_allow_non_publishable=true in resolved_config.json then runs release.
set -euo pipefail

REPO_ROOT="/home/savitasajwan03/option_trading"
PYTHON="${REPO_ROOT}/.venv/bin/python"
C1_DIR="${REPO_ROOT}/ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848"

echo "== Patching resolved_config.json =="
"${PYTHON}" - <<'PY'
import json, pathlib, sys
p = pathlib.Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848/resolved_config.json")
d = json.loads(p.read_text())
d.setdefault("publish", {})["smoke_allow_non_publishable"] = True
p.write_text(json.dumps(d, indent=2))
print("patched:", p)
PY

echo "== Running force-publish directly (bypasses --run-dir flag bug) =="
PYTHONPATH="${REPO_ROOT}" "${PYTHON}" - <<'PY' 2>&1 | tee /home/savitasajwan03/option_trading/ml_pipeline_2/tools/c1_publish.log
import sys, json
sys.path.insert(0, "/home/savitasajwan03/option_trading")
from ml_pipeline_2.staged.publish import assess_staged_release_candidate, publish_staged_run

C1_DIR = "/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848"
MODEL_GROUP = "banknifty_futures/h15_tp_auto"
PROFILE_ID = "openfe_v9_dual"

print("== Assessing with force_publish_nonpublishable=True ==")
assessment = assess_staged_release_candidate(run_dir=C1_DIR, force_publish_nonpublishable=True)
print("publishable:", assessment["publishable"])
print("decision:", assessment["decision"])
print("blocking (informational):", assessment.get("blocking_reasons", []))

if not assessment["publishable"]:
    print("ERROR: still not publishable after force flag — check integrity")
    sys.exit(1)

print("\n== Publishing bundle locally (no GCS) ==")
result = publish_staged_run(
    run_dir=C1_DIR,
    model_group=MODEL_GROUP,
    profile_id=PROFILE_ID,
    force_publish_nonpublishable=True,
)
print("publish_status:", result.get("publish_status"))
print("published_paths:")
for k, v in (result.get("published_paths") or {}).items():
    print(f"  {k}: {v}")
PY

echo "== Done. Check published_models: =="
ls "${REPO_ROOT}/ml_pipeline_2/artifacts/published_models/" 2>/dev/null || echo "(empty or missing)"
