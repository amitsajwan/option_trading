#!/usr/bin/env bash
# Hard preflight before overnight ML playground — exit non-zero on any blocker.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}"

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
PLAYGROUND_MODE="${PLAYGROUND_MODE:-all}"

ENTRY_HPO_MANIFEST="${ENTRY_HPO_MANIFEST:-ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_only_hpo_v2.json}"
DIR_HPO_MANIFEST="${DIR_HPO_MANIFEST:-ml_pipeline_2/configs/research/staged_dual_recipe.direction_s2_only_hpo_v2.json}"
ENTRY_GRID_MANIFEST="${ENTRY_GRID_MANIFEST:-ml_pipeline_2/configs/research/staged_grid.entry_playground_v1.json}"
DIR_GRID_MANIFEST="${DIR_GRID_MANIFEST:-ml_pipeline_2/configs/research/staged_grid.direction_playground_v1.json}"

PARQUET_ROOT="${PARQUET_ROOT:-${REPO_ROOT}/.data/ml_pipeline/parquet_data}"
MIN_FREE_GB="${MIN_FREE_GB:-25}"

_fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }
_ok() { echo "PREFLIGHT OK: $*"; }

[[ -x "${VENV_PYTHON}" ]] || _fail "missing venv python at ${VENV_PYTHON}"
_ok "venv python"

"${VENV_PYTHON}" - <<'PY' || _fail "import check (xgboost/lightgbm/optuna/pandas)"
import optuna  # noqa: F401
import pandas  # noqa: F401
import xgboost  # noqa: F401
try:
    import lightgbm  # noqa: F401
except ImportError as exc:
    raise SystemExit(f"lightgbm: {exc}") from exc
print("imports OK")
PY

ds="${PARQUET_ROOT}/snapshots_ml_flat_v2"
[[ -d "${ds}" ]] || _fail "missing dataset ${ds}"
if ! find "${ds}" -name '*.parquet' -print -quit 2>/dev/null | grep -q .; then
  _fail "no parquet under ${ds}"
fi
_ok "parquet snapshots_ml_flat_v2"

free_gb="$(df -BG "${REPO_ROOT}" | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
[[ "${free_gb}" -ge "${MIN_FREE_GB}" ]] || _fail "disk free ${free_gb}GB < ${MIN_FREE_GB}GB required"
_ok "disk ${free_gb}GB free"

# Do not check orchestrator.pid here — start writes it before calling preflight.
for pidfile in /tmp/entry_s1_only_hpo.pid /tmp/direction_s2_only_hpo.pid; do
  if [[ -f "${pidfile}" ]]; then
    pid="$(cat "${pidfile}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      _fail "another ML job still running (pidfile ${pidfile} pid=${pid})"
    fi
    rm -f "${pidfile}" 2>/dev/null || sudo rm -f "${pidfile}" 2>/dev/null || true
  fi
done
_ok "no conflicting pidfiles"

"${VENV_PYTHON}" -m ml_pipeline_2.scripts.run_entry_s1_only_hpo \
  --config "${ENTRY_HPO_MANIFEST}" --validate-only
_ok "entry HPO manifest"

"${VENV_PYTHON}" -m ml_pipeline_2.scripts.run_direction_s2_only_hpo \
  --config "${DIR_HPO_MANIFEST}" --validate-only
_ok "direction HPO manifest"

if [[ "${PLAYGROUND_MODE}" == "grid" || "${PLAYGROUND_MODE}" == "all" ]]; then
  "${VENV_PYTHON}" - <<PY
from pathlib import Path
from ml_pipeline_2.contracts.manifests import STAGED_GRID_KIND, load_and_resolve_manifest
from ml_pipeline_2.experiment_control.runner import validate_runtime_environment

repo = Path("${REPO_ROOT}")
for rel in ["${ENTRY_GRID_MANIFEST}", "${DIR_GRID_MANIFEST}"]:
    grid = load_and_resolve_manifest(repo / rel, validate_paths=True)
    assert grid.get("experiment_kind") == STAGED_GRID_KIND, rel
    base = grid["inputs"]["base_manifest_path"]
    base_resolved = load_and_resolve_manifest((repo / "ml_pipeline_2/configs/research" / base).resolve(), validate_paths=True)
    validate_runtime_environment(base_resolved)
    print("grid OK:", rel, "base:", base)
PY
  _ok "entry+direction grid manifests"
fi

mem_avail_kb="$(grep MemAvailable /proc/meminfo | awk '{print $2}')"
mem_avail_gb=$((mem_avail_kb / 1024 / 1024))
[[ "${mem_avail_gb}" -ge 20 ]] || _fail "MemAvailable ${mem_avail_gb}GB < 20GB (stop compose first)"
_ok "RAM ${mem_avail_gb}GB available"

echo "PREFLIGHT PASSED mode=${PLAYGROUND_MODE}"
