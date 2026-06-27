import subprocess

containers = [
    "option_trading-ingestion_app-1",
    "option_trading-strategy_app-1", 
    "option_trading-execution_app-1",
    "option_trading-snapshot_app-1"
]

for c in containers:
    print(f"{c}:")
    r = subprocess.run(f"sudo docker exec {c} printenv | grep INSTRUMENT", shell=True, capture_output=True, text=True)
    print(r.stdout.strip() or "NOT_SET")
