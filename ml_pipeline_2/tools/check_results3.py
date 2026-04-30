"""Deep-dump A2/A3 summary structure to find actual holdout metrics."""
import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")

def deep_show(d, prefix="", max_depth=4, depth=0):
    if depth >= max_depth:
        return
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                print(f"{prefix}{k}:")
                deep_show(v, prefix + "  ", max_depth, depth + 1)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"{prefix}{k}: [list of {len(v)} dicts]")
            else:
                val_str = str(v)[:100]
                print(f"{prefix}{k}: {val_str}")
    else:
        print(f"{prefix}{d}"[:120])

for name, dirname in [("A2", "staged_label_fix_a2_market_direction"),
                       ("A3", "staged_label_fix_a3_combined")]:
    sp = BASE / dirname / "summary.json"
    s = json.loads(sp.read_text())
    print(f"\n{'='*70}")
    print(f"{name} — full nested structure (depth=3)")
    print(f"{'='*70}")
    print(f"completion_mode: {s.get('completion_mode')}")
    print(f"publish_assessment: {s.get('publish_assessment')}")
    print()

    # Focus on cv_prechecks, training_environment, scenario_reports
    for key in ["cv_prechecks", "training_environment", "scenario_reports"]:
        v = s.get(key)
        if v:
            print(f"--- {key} ---")
            deep_show(v, "  ", max_depth=4)
            print()
