import os

ENV_FILE = "/opt/option_trading/.env.compose"

with open(ENV_FILE) as f:
    lines = f.readlines()

updated = []
for line in lines:
    # Uncomment EXECUTION_ADAPTER if commented
    if line.strip().startswith("# EXECUTION_ADAPTER="):
        updated.append("EXECUTION_ADAPTER=dhan\n")
    else:
        updated.append(line)

with open(ENV_FILE, "w") as f:
    f.writelines(updated)

print("EXECUTION_ADAPTER=dhan uncommented/set")

# Verify
with open(ENV_FILE) as f:
    for line in f:
        if line.startswith("EXECUTION_ADAPTER="):
            print(f"Confirmed: {line.strip()}")
