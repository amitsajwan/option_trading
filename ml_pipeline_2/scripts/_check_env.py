import subprocess, json
out = subprocess.check_output(["docker", "inspect", "option_trading-strategy_app_historical-1"])
c = json.loads(out)[0]
env = c["Config"]["Env"]
for e in env:
    if "OPTION_PNL" in e or "ML_PURE" in e or "BUNDLE" in e:
        print(e)
