import os

ENV_FILE = "/opt/option_trading/.env.compose"

with open(ENV_FILE) as f:
    lines = f.readlines()

# Keep only the last EXECUTION_ADAPTER line
seen_adapter = False
updated = []
for line in reversed(lines):
    if line.startswith("EXECUTION_ADAPTER="):
        if not seen_adapter:
            seen_adapter = True
            updated.append(line)
    else:
        updated.append(line)

updated = list(reversed(updated))

with open(ENV_FILE, "w") as f:
    f.writelines(updated)

print("Deduplicated EXECUTION_ADAPTER")
