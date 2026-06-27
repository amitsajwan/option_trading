import sys
sys.path.insert(0, "/opt/option_trading")

from pathlib import Path
f = Path("/opt/option_trading/market_data_dashboard/routes/sim_routes.py")
print("exists:", f.exists())
print("size:", f.stat().st_size)
text = f.read_text()
print("RISK_MAX_DAILY_LOSS_PCT present:", "RISK_MAX_DAILY_LOSS_PCT" in text)
print("RISK_MAX_SESSION_TRADES present:", "RISK_MAX_SESSION_TRADES" in text)
print("SIDEWAYS_RETURNS_MIXED_GATE_ENABLED present:", "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED" in text)
