import subprocess, sys

# Get latest strategy_app_sim container name
result = subprocess.run(
    ["sudo", "docker", "ps", "-a", "--format", "{{.Names}}"],
    capture_output=True, text=True
)
containers = [c for c in result.stdout.strip().split("\n") if "strategy_app_sim-run" in c]
if not containers:
    print("No strategy_app_sim containers found")
    sys.exit(1)

latest = containers[0]
print(f"Checking logs for: {latest}")

result = subprocess.run(
    ["sudo", "docker", "logs", latest, "--tail", "30"],
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
