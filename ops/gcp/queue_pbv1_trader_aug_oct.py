#!/usr/bin/env python3
"""Queue Aug–Oct replay for PBV1_TOP3_TRADER_V1 after preflight + env patch."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8008/api/strategy/evaluation/runs"
DATE_FROM = "2024-08-01"
DATE_TO = "2024-10-31"
TRADER_RULE = "/app/ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_trader_v1.json"


def main() -> int:
    patch = subprocess.run(
        ["bash", "ops/gcp/patch_playbook_v1_env.sh", ".env.compose", TRADER_RULE],
        cwd="/opt/option_trading",
        capture_output=True,
        text=True,
        check=False,
    )
    print(patch.stdout or "", flush=True)
    if patch.returncode != 0:
        print(patch.stderr or "", flush=True)
        return patch.returncode

    rebuild = subprocess.run(
        [
            "sudo",
            "docker",
            "compose",
            "--env-file",
            ".env.compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.gcp.yml",
            "build",
            "strategy_app_historical",
        ],
        cwd="/opt/option_trading",
        capture_output=True,
        text=True,
        check=False,
    )
    print((rebuild.stdout or "")[-2000:], flush=True)
    if rebuild.returncode != 0:
        print((rebuild.stderr or "")[-2000:], flush=True)
        return rebuild.returncode

    up = subprocess.run(
        [
            "sudo",
            "docker",
            "compose",
            "--env-file",
            ".env.compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.gcp.yml",
            "--profile",
            "historical",
            "up",
            "-d",
            "--force-recreate",
            "strategy_app_historical",
        ],
        cwd="/opt/option_trading",
        capture_output=True,
        text=True,
        check=False,
    )
    print(up.stdout or "", flush=True)
    if up.returncode != 0:
        print(up.stderr or "", flush=True)
        return up.returncode

    pre = subprocess.run(
        [sys.executable, "ops/gcp/preflight_historical_replay.py"],
        cwd="/opt/option_trading",
        capture_output=True,
        text=True,
        check=False,
    )
    print(pre.stdout or "", flush=True)
    if pre.returncode != 0:
        print("abort: preflight failed", flush=True)
        return pre.returncode

    payload = json.dumps(
        {"dataset": "historical", "date_from": DATE_FROM, "date_to": DATE_TO, "speed": 0}
    ).encode()
    req = urllib.request.Request(
        API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    run_id = str(result.get("run_id") or "").strip()
    print(json.dumps(result), flush=True)
    if not run_id:
        return 1

    for i in range(360 * 2):
        try:
            with urllib.request.urlopen(f"{API}/{run_id}", timeout=30) as resp:
                status = str(json.loads(resp.read().decode()).get("status") or "").lower()
        except urllib.error.URLError as exc:
            status = f"error:{exc}"
        print(f"poll {i} status={status}", flush=True)
        if status in {"completed", "failed", "cancelled"}:
            print(
                f"EVAL_LINK=http://34.93.40.198:8008/app/?mode=eval&run_id={run_id}"
                f"&date_from={DATE_FROM}&date_to={DATE_TO}",
                flush=True,
            )
            return 0 if status == "completed" else 1
        time.sleep(30)
    return 1


if __name__ == "__main__":
    sys.exit(main())
