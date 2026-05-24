"""Build rule_matrix_r1s_top3_monthly.json from generated month windows."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WINDOWS_PATH = ROOT / "_monthly_windows_2020_2024.json"
OUT_PATH = ROOT / "rule_matrix_r1s_top3_monthly.json"


def main() -> None:
    windows = json.loads(WINDOWS_PATH.read_text(encoding="utf-8"))
    matrix = {
        "description": (
            "R1S top-3 S3 composite — calendar month windows 2020-08 through 2024-10. "
            f"{len(windows)} months x 1 rule = {len(windows)} cells."
        ),
        "rules": [
            {
                "rule_id": "R1S_TOP3_S3_COMPOSITE",
                "path": "ml_pipeline_2/configs/rules/r1s_top3/r1s_top3_s3_composite.json",
            },
            {
                "rule_id": "R1S_UNLIMITED_CONTROL",
                "path": "ml_pipeline_2/configs/rules/r1s_top3/r1s_unlimited_control.json",
            },
        ],
        "windows": windows,
        "exit_modes": ["mechanical"],
        "audit_thresholds": {
            "min_trades": 10,
            "max_trades": 80,
            "min_win_rate": 0.40,
            "t_min": 2.0,
            "ci_must_exclude_zero": True,
            "outlier_survival_must_be_nonneg": True,
        },
    }
    OUT_PATH.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    n_cells = len(windows) * len(matrix["rules"])
    print(f"wrote {OUT_PATH} ({len(windows)} months, {n_cells} cells)")


if __name__ == "__main__":
    main()
