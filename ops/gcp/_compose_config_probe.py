#!/usr/bin/env python3
import subprocess
import sys

sys.path.insert(0, "/opt/option_trading")
from sim_orchestrator.main import _compose_run_env

rid = "e709546a-6cb6-4651-ac60-a3b2fcd74eca"
overrides = {
    "STRATEGY_PROFILE_ID": "debit_multi_v1",
    "DEPTH_FEED_ENABLED": "1",
    "ENTRY_ML_MIN_PROB": "0.65",
    "ML_ENTRY_BLOCK_CE": "0",
    "ML_ENTRY_BLOCK_PE": "0",
}
env = _compose_run_env(rid, overrides)
print("env STRATEGY_PROFILE_ID =", env.get("STRATEGY_PROFILE_ID"))
cmd = [
    "docker",
    "compose",
    "-f",
    "/opt/option_trading/docker-compose.yml",
    "-f",
    "/opt/option_trading/docker-compose.gcp.yml",
    "--profile",
    "sim",
    "config",
]
out = subprocess.run(cmd, env=env, cwd="/opt/option_trading", capture_output=True, text=True, check=False)
text = out.stdout + out.stderr
for line in text.splitlines():
    if "strategy-profile-id" in line or "STRATEGY_PROFILE_ID" in line:
        print(line)
