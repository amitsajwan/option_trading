ENV_FILE = "/opt/option_trading/.env.compose"

with open(ENV_FILE) as f:
    lines = f.readlines()

updated = []
found = False
for line in lines:
    if line.startswith("RISK_MAX_DAILY_LOSS_PCT="):
        updated.append("RISK_MAX_DAILY_LOSS_PCT=0.12\n")
        found = True
    else:
        updated.append(line)

if not found:
    updated.append("RISK_MAX_DAILY_LOSS_PCT=0.12\n")

with open(ENV_FILE, "w") as f:
    f.writelines(updated)

print("RISK_MAX_DAILY_LOSS_PCT set to 0.12 (12%)")

# verify
with open(ENV_FILE) as f:
    for line in f:
        if line.startswith("RISK_MAX_DAILY_LOSS_PCT="):
            print("Confirmed:", line.strip())
