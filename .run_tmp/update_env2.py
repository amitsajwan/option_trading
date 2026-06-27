import os, sys

ENV_FILE = "/opt/option_trading/.env.compose"
TMP_FILE = "/tmp/.env.compose.new"

# Read existing
with open(ENV_FILE) as f:
    lines = f.readlines()

# Remove existing RISK_MAX_SESSION_TRADES lines
lines = [l for l in lines if not l.strip().startswith("RISK_MAX_SESSION_TRADES=")]

# Append new settings at end
lines.append("\n# --- Tuned for more trades ---\n")
lines.append("RISK_MAX_SESSION_TRADES=20\n")

with open(TMP_FILE, "w") as f:
    f.writelines(lines)

os.system(f"sudo mv {TMP_FILE} {ENV_FILE}")
os.system(f"sudo chown ubuntu:ubuntu {ENV_FILE}")

print("Updated .env.compose")
print("RISK_MAX_SESSION_TRADES=20")
