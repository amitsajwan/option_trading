import json
import subprocess
import sys
import time
from pathlib import Path

VM_HOST = "34.47.131.234"
VM_USER = "savitasajwan03"
SSH_KEY = r"C:\Users\amits\.ssh\google_compute_engine"
KNOWN_HOSTS = r"C:\Users\amits\.ssh\known_hosts"

INFO_FILE = Path(__file__).with_name("bypass_stage2_launch_info.json")


def ssh_cmd(remote_cmd: str) -> list[str]:
    return [
        "ssh", "-i", SSH_KEY,
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=yes",
        f"{VM_USER}@{VM_HOST}",
        remote_cmd,
    ]


def run_ssh(remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_cmd(remote_cmd), capture_output=True, text=True, timeout=timeout)


def load_info() -> dict:
    if not INFO_FILE.exists():
        print(f"Info file not found: {INFO_FILE}")
        sys.exit(1)
    return json.loads(INFO_FILE.read_text())


def discover_run_dir(prefix: str = "expiry_bypass_stage2_test_v1_") -> str | None:
    r = run_ssh(
        f"ls -td /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/{prefix}* 2>/dev/null | head -n 1"
    )
    if r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    return None


def main():
    info = load_info()
    pid = info.get("pid")
    run_dir = info.get("run_dir")

    # Always discover most recent run_dir
    discovered = discover_run_dir()
    if discovered and discovered != run_dir:
        run_dir = discovered
        info["run_dir"] = run_dir
        INFO_FILE.write_text(json.dumps(info, indent=2))

    print(f"Run directory: {run_dir}")

    # Check tmux session
    r = run_ssh("tmux ls 2>/dev/null | grep bypass_stage2 || echo 'NO TMUX SESSION'")
    print(f"Tmux status:\n{r.stdout.strip()}")

    # Check python process
    r = run_ssh("ps aux | grep '[m]l_pipeline_2.run_research' | awk '{print \$2,\$3,\$4,\$11}'")
    if r.stdout.strip():
        print(f"\nPython processes:\n{r.stdout.strip()}")
    else:
        print("\nNo ml_pipeline_2.run_research processes found.")

    # Check run_status.json
    if run_dir:
        r = run_ssh(f"cat {run_dir}/run_status.json 2>/dev/null")
        if r.returncode == 0 and r.stdout.strip():
            try:
                status = json.loads(r.stdout)
                print(f"\nRun status: {status.get('status')}")
                print(f"Active stage: {status.get('active_stage')}")
                print(f"Last event: {status.get('last_progress_event')}")
                print(f"Updated: {status.get('updated_at_utc')}")
                if status.get("last_progress_payload"):
                    print(f"Progress: {json.dumps(status.get('last_progress_payload'), indent=2)}")
            except json.JSONDecodeError:
                print(f"Run status raw:\n{r.stdout[:500]}")

    # Check state.jsonl
    if run_dir:
        r = run_ssh(f"wc -l {run_dir}/state.jsonl 2>/dev/null; echo '---TAIL---'; tail -n 5 {run_dir}/state.jsonl 2>/dev/null")
        if r.stdout.strip():
            print(f"\nState events:\n{r.stdout.strip()}")

    # Check summary.json
    if run_dir:
        r = run_ssh(f"ls -la {run_dir}/summary.json 2>/dev/null || echo 'NO SUMMARY YET'")
        print(f"\nSummary: {r.stdout.strip()}")

    # Check latest tmux log
    r = run_ssh("ls -lt /home/savitasajwan03/option_trading/logs/bypass_stage2_tmux_*.log 2>/dev/null | head -n 1")
    if r.stdout.strip():
        latest_log = r.stdout.strip().splitlines()[0].split()[-1]
        print(f"\nLatest tmux log: {latest_log}")
        r2 = run_ssh(f"tail -n 20 {latest_log} 2>/dev/null")
        if r2.stdout.strip():
            print(r2.stdout.strip()[:2000])


if __name__ == "__main__":
    raise SystemExit(main())
