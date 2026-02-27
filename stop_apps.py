from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop option_trading stack via Docker Compose.")
    parser.add_argument("--env-file", default=".env.compose")
    parser.add_argument("--volumes", action="store_true", help="Also remove compose volumes.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    env_file = Path(str(args.env_file)).resolve()
    if not env_file.exists():
        raise FileNotFoundError(f"env file not found: {env_file}")

    cmd = ["docker", "compose", "--env-file", str(env_file), "down", "--remove-orphans"]
    if bool(args.volumes):
        cmd.append("--volumes")

    print("[stop_apps] Command:", " ".join(cmd))
    if bool(args.dry_run):
        return 0

    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(run_cli())
