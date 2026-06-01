#!/usr/bin/env python3
import sys

sys.path.insert(0, "/opt/option_trading")
from sim_orchestrator.main import _compose_run_env, spawn_consumer
from unittest.mock import patch

overrides = {"STRATEGY_PROFILE_ID": "debit_multi_v1"}
env = _compose_run_env("test-run", overrides)
print("STRATEGY_PROFILE_ID=", env.get("STRATEGY_PROFILE_ID"))
print("SIM_RUN_ID=", env.get("SIM_RUN_ID"))

with patch("sim_orchestrator.main.subprocess.run") as run:
    try:
        spawn_consumer("test-run", env_overrides=overrides)
    except Exception as exc:
        print("spawn_consumer error:", exc)
    if run.called:
        cmd = run.call_args.args[0]
        print("cmd has --env-file:", "--env-file" in cmd)
        print("cmd snippet:", " ".join(cmd[:20]))
