import base64
import json
import subprocess
from pathlib import Path

# Config
VM_HOST = "34.47.131.234"
VM_USER = "savitasajwan03"
SSH_KEY = r"C:\Users\amits\.ssh\google_compute_engine"
KNOWN_HOSTS = r"C:\Users\amits\.ssh\known_hosts"
LOCAL_CONFIG = Path(__file__).parent / "ml_pipeline_2" / "configs" / "research" / "staged_single_run.expiry_bypass_stage2.json"
REMOTE_CONFIG_DIR = "/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research"
REMOTE_VENV_PYTHON = "/home/savitasajwan03/option_trading/.venv/bin/python"
REMOTE_MODULE = "ml_pipeline_2.run_research"

def ssh_cmd(remote_cmd: str) -> list[str]:
    return [
        "ssh",
        "-i", SSH_KEY,
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=yes",
        f"{VM_USER}@{VM_HOST}",
        remote_cmd,
    ]

def run_local(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def main():
    config_path = LOCAL_CONFIG.resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    config_bytes = config_path.read_bytes()
    config_b64 = base64.b64encode(config_bytes).decode()
    remote_config_path = f"{REMOTE_CONFIG_DIR}/staged_single_run.expiry_bypass_stage2.json"

    # Write config to VM via base64-encoded heredoc
    write_cmd = f"""base64 -d << 'EOF' > {remote_config_path}
{config_b64}
EOF"""
    print("Writing config to VM...")
    r = run_local(ssh_cmd(write_cmd), timeout=30)
    if r.returncode != 0:
        print(f"FAILED to write config:\n{r.stderr}")
        return 1
    print("Config written.")

    # Validate config on VM
    validate_cmd = (
        f"cd /home/savitasajwan03/option_trading && "
        f"PYTHONPATH=/home/savitasajwan03/option_trading "
        f"{REMOTE_VENV_PYTHON} -m {REMOTE_MODULE} "
        f"--config {remote_config_path} --validate-only"
    )
    print("Validating config on VM...")
    r = run_local(ssh_cmd(validate_cmd), timeout=120)
    print(f"Validation stdout:\n{r.stdout}")
    if r.returncode != 0:
        print(f"Validation FAILED:\n{r.stderr}")
        return 1
    print("Config validated successfully on VM.")

    # Launch training in background via a wrapper script
    timestamp = subprocess.run(["python", "-c", "import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S'))"], capture_output=True, text=True).stdout.strip()
    log_file = f"/home/savitasajwan03/option_trading/logs/bypass_stage2_{timestamp}.log"
    wrapper_script = (
        f"#!/bin/bash\n"
        f"cd /home/savitasajwan03/option_trading\n"
        f"export PYTHONPATH=/home/savitasajwan03/option_trading\n"
        f"nohup {REMOTE_VENV_PYTHON} -m {REMOTE_MODULE} "
        f"--config {remote_config_path} "
        f"> {log_file} 2>&1 &\n"
        f"echo $!\n"
    )
    wrapper_b64 = base64.b64encode(wrapper_script.encode()).decode()
    wrapper_path = f"/tmp/launch_bypass_stage2_{timestamp}.sh"

    # Write wrapper
    write_wrapper = f"base64 -d << 'EOF' > {wrapper_path} && chmod +x {wrapper_path}\n{wrapper_b64}\nEOF"
    r = run_local(ssh_cmd(write_wrapper), timeout=30)
    if r.returncode != 0:
        print(f"FAILED to write wrapper:\n{r.stderr}")
        return 1

    # Execute wrapper (this returns immediately because the script itself backgrounds)
    print(f"Launching training (log: {log_file})...")
    r = run_local(ssh_cmd(f"bash {wrapper_path}"), timeout=60)
    if r.returncode != 0:
        print(f"Launch FAILED:\n{r.stderr}")
        return 1
    pid = r.stdout.strip().splitlines()[-1].strip()
    print(f"Training launched with PID {pid} on VM.")
    print(f"Monitor: ssh -i {SSH_KEY} {VM_USER}@{VM_HOST} 'tail -f {log_file}'")
    print(f"Kill: ssh -i {SSH_KEY} {VM_USER}@{VM_HOST} 'kill {pid}'")

    # Save launch info locally
    launch_info = {
        "pid": pid,
        "log_file": log_file,
        "config_path": str(remote_config_path),
        "launched_at_utc": timestamp,
    }
    info_path = Path(__file__).with_name("bypass_stage2_launch_info.json")
    info_path.write_text(json.dumps(launch_info, indent=2))
    print(f"Launch info saved to {info_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
