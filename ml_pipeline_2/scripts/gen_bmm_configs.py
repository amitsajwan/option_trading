"""Generate the Lean-5 BMM (Big-Move Model) multi-horizon training configs.

Each config is the entry_s1_v3_5m_020pct recipe (the live AUC-0.831 baseline) with
ONLY two axes changed so the comparison is clean:
  1. feature set  -> fo_bmm_v1  (adds the compression/stored-energy/structure features)
  2. label        -> per-horizon move threshold (the multi-horizon BMM)

Everything else (windows, HPO budget, models, gates) is identical to v3 so any AUC
delta is attributable to the new features + horizon, not a window/HPO change.

Run:  python ml_pipeline_2/scripts/gen_bmm_configs.py
Writes 5 configs into ml_pipeline_2/configs/research/staged_dual_recipe.bmm_*.json
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CONFIG_DIR = _HERE.parent / "configs" / "research"
_BASE = _CONFIG_DIR / "staged_dual_recipe.entry_s1_v3_5m_020pct.json"

# (horizon_minutes, min_pct, min_points, run_name)  — Lean-5 matrix.
# min_points ~ min_pct * 54000 (BankNifty spot scale), rounded to the nearest convenient pt.
MATRIX = [
    (5,  0.0010, 54,  "bmm_h05m_010pct"),
    (10, 0.0015, 80,  "bmm_h10m_015pct"),
    (15, 0.0020, 108, "bmm_h15m_020pct"),
    (20, 0.0030, 160, "bmm_h20m_030pct"),
    (30, 0.0040, 216, "bmm_h30m_040pct"),
]

FEATURE_SET = "fo_bmm_v1"


def main() -> int:
    base = json.loads(_BASE.read_text())
    written = []
    for horizon, min_pct, min_points, run_name in MATRIX:
        cfg = copy.deepcopy(base)
        cfg["_comment"] = (
            f"BMM big-move model. horizon={horizon}m, label move>={min_pct*100:.2f}% "
            f"(~{min_points}pt). Feature set {FEATURE_SET} (adds compression/energy/structure). "
            f"Same windows/HPO/models as entry_s1_v3 so feature+horizon effect is isolated."
        )
        cfg["outputs"]["run_name"] = run_name
        # Feature set on every stage (stage2/3 bypassed but keep consistent).
        for stage in ("stage1", "stage2", "stage3"):
            cfg["catalog"]["feature_sets_by_stage"][stage] = [FEATURE_SET]
        # Label: horizon + per-horizon move threshold.
        cfg["labels"]["stage1_entry_move"] = {
            "horizon_minutes": horizon,
            "min_pct": min_pct,
            "min_points": min_points,
        }
        out = _CONFIG_DIR / f"staged_dual_recipe.{run_name}.json"
        out.write_text(json.dumps(cfg, indent=2) + "\n")
        written.append(out.name)
    print("wrote:")
    for name in written:
        print("  ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
