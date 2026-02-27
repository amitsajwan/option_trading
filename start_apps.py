from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


CORE_SERVICES: List[str] = [
    "redis",
    "mongo",
    "ingestion_app",
    "snapshot_app",
    "persistence_app",
    "strategy_app",
]


def _resolve_env_file(path: str) -> Path:
    env_file = Path(path).resolve()
    if env_file.exists():
        return env_file
    example = Path(".env.compose.example").resolve()
    if example.exists() and env_file.name == ".env.compose":
        shutil.copyfile(example, env_file)
        print(f"[start_apps] Created env file from template: {env_file}")
        return env_file
    raise FileNotFoundError(f"env file not found: {env_file}")


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start option_trading stack via Docker Compose.")
    parser.add_argument("--env-file", default=".env.compose")
    parser.add_argument("--include-dashboard", action="store_true", help="Also start dashboard service (ui profile).")
    parser.add_argument("--include-historical", action="store_true", help="Also start historical replay service.")
    parser.add_argument("--no-build", action="store_true", help="Skip image build step.")
    parser.add_argument(
        "--no-legacy-builder",
        action="store_true",
        help="Do not force DOCKER_BUILDKIT=0 / COMPOSE_DOCKER_CLI_BUILD=0.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    env_file = _resolve_env_file(str(args.env_file))
    cmd: List[str] = ["docker", "compose", "--env-file", str(env_file)]

    services = list(CORE_SERVICES)
    if bool(args.include_dashboard):
        cmd.extend(["--profile", "ui"])
        services.append("dashboard")
    if bool(args.include_historical):
        cmd.extend(["--profile", "historical"])
        services.append("historical_replay")

    cmd.extend(["up", "-d"])
    if not bool(args.no_build):
        cmd.append("--build")
    cmd.extend(services)

    env = os.environ.copy()
    if not bool(args.no_legacy_builder):
        env["DOCKER_BUILDKIT"] = "0"
        env["COMPOSE_DOCKER_CLI_BUILD"] = "0"

    print("[start_apps] Command:", " ".join(cmd))
    if bool(args.dry_run):
        return 0

    proc = subprocess.run(cmd, env=env, check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(run_cli())
