import json, subprocess, sys
try:
    out = subprocess.run(["docker","inspect","option_trading-strategy_app-1"], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    for env in d[0]["Config"]["Env"]:
        if any(k in env for k in ["REDIS","ENTRY_ML","ENTRY_VOL","STRATEGY_PROFILE"]):
            print(env)
    print("---NETWORKS---")
    nets = d[0].get("NetworkSettings",{}).get("Networks",{})
    for name, net in nets.items():
        print(f"  {name}: {net.get('IPAddress','none')}")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
