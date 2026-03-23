#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env.compose}"
RELEASE_ENV_PATH="${RELEASE_ENV_PATH:-}"
AUTO_PUBLISH_RUNTIME_CONFIG="${AUTO_PUBLISH_RUNTIME_CONFIG:-0}"

if [ -z "${RELEASE_ENV_PATH}" ]; then
  echo "Set RELEASE_ENV_PATH to the release/ml_pure_runtime.env file from the staged or recovery release flow." >&2
  exit 1
fi

if [ ! -f "${RELEASE_ENV_PATH}" ]; then
  echo "Release env file not found: ${RELEASE_ENV_PATH}" >&2
  exit 1
fi

if [ ! -f "${ENV_FILE}" ]; then
  echo "Compose env file not found: ${ENV_FILE}" >&2
  exit 1
fi

export RELEASE_ENV_PATH
export ENV_FILE

python - <<'PY'
from pathlib import Path
import os

release_env_path = Path(os.environ["RELEASE_ENV_PATH"]).resolve()
env_file = Path(os.environ["ENV_FILE"]).resolve()

release_values = {}
for raw_line in release_env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    release_values[key.strip()] = value.strip()

required = ["STRATEGY_ENGINE", "ML_PURE_RUN_ID", "ML_PURE_MODEL_GROUP"]
missing = [key for key in required if not release_values.get(key)]
if missing:
    raise SystemExit(f"release env missing keys: {', '.join(missing)}")

env_values = {}
env_lines = []
for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.rstrip("\n")
    env_lines.append(line)
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        continue
    key, value = stripped.split("=", 1)
    env_values[key.strip()] = value

updates = {
    "STRATEGY_ENGINE": release_values["STRATEGY_ENGINE"],
    "ML_PURE_RUN_ID": release_values["ML_PURE_RUN_ID"],
    "ML_PURE_MODEL_GROUP": release_values["ML_PURE_MODEL_GROUP"],
    "ML_PURE_MODEL_PACKAGE": "",
    "ML_PURE_THRESHOLD_REPORT": "",
}

seen = set()
out_lines = []
for raw_line in env_lines:
    stripped = raw_line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in updates:
            out_lines.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out_lines.append(raw_line)

for key, value in updates.items():
    if key not in seen:
        out_lines.append(f"{key}={value}")

env_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print(f"Updated {env_file}")
print(f"  STRATEGY_ENGINE={updates['STRATEGY_ENGINE']}")
print(f"  ML_PURE_RUN_ID={updates['ML_PURE_RUN_ID']}")
print(f"  ML_PURE_MODEL_GROUP={updates['ML_PURE_MODEL_GROUP']}")
PY

if [ "${AUTO_PUBLISH_RUNTIME_CONFIG}" = "1" ]; then
  if [ -z "${RUNTIME_CONFIG_BUCKET_URL:-}" ]; then
    echo "AUTO_PUBLISH_RUNTIME_CONFIG=1 requires RUNTIME_CONFIG_BUCKET_URL." >&2
    exit 1
  fi
  echo "Publishing updated runtime config bundle to ${RUNTIME_CONFIG_BUCKET_URL}"
  "${REPO_ROOT}/ops/gcp/publish_runtime_config.sh"
fi

echo "ML pure release handoff applied successfully."
