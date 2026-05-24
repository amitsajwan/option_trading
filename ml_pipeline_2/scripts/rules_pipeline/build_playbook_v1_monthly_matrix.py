"""Build rule_matrix_playbook_v1_monthly.json after smoke picks a winner."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WINDOWS_PATH = ROOT / "_monthly_windows_2020_2024.json"
OUT_PATH = ROOT / "rule_matrix_playbook_v1_monthly.json"

# Update after smoke — default runs all PBV1 variants + control.
PBV1_RULES = [
    {"rule_id": "R1S_TOP3_S3_COMPOSITE", "path": "ml_pipeline_2/configs/rules/r1s_top3/r1s_top3_s3_composite.json"},
    {"rule_id": "PBV1_TOP3_THESIS", "path": "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis.json"},
    {"rule_id": "PBV1_TOP3_THESIS_TRAIL", "path": "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis_trail.json"},
    {"rule_id": "PBV1_TOP3_CALM_THESIS", "path": "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_calm_thesis.json"},
    {"rule_id": "PBV1_TOP3_QUALITY_THESIS", "path": "ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_quality_thesis.json"},
]


def main() -> None:
    windows = json.loads(WINDOWS_PATH.read_text(encoding="utf-8"))
    matrix = {
        "description": "Playbook v1 monthly audit — thesis/trail/calm/quality vs R1S.",
        "rules": PBV1_RULES,
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
    print(f"wrote {OUT_PATH} ({len(windows)} months, {len(windows) * len(PBV1_RULES)} cells)")


if __name__ == "__main__":
    main()
