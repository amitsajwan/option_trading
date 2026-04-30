import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")

# Print top-level keys and nested structure of A1 summary
sp = BASE / "staged_label_fix_a1_window_shift" / "summary.json"
s = json.loads(sp.read_text())
print("TOP-LEVEL KEYS:", list(s.keys()))
print()
for k, v in s.items():
    if isinstance(v, dict):
        print(f"  {k}: {list(v.keys())[:8]}")
    else:
        print(f"  {k}: {str(v)[:80]}")
