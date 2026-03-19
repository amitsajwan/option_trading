from __future__ import annotations

import json
from pathlib import Path

from ml_pipeline_2.catalog import default_staged_manifest_payload


def test_default_staged_manifest_matches_checked_in_default() -> None:
    payload = json.loads(Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json").read_text(encoding="utf-8"))
    defaults = default_staged_manifest_payload()

    assert defaults["catalog"]["feature_sets_by_stage"]["stage1"] == ["fo_expiry_aware_v2"]
    assert defaults["catalog"]["feature_sets_by_stage"]["stage3"] == ["fo_full"]
    assert payload["catalog"]["feature_sets_by_stage"] == defaults["catalog"]["feature_sets_by_stage"]
    assert payload["inputs"] == {
        "parquet_root": "../../../.data/ml_pipeline/parquet_data",
        "support_dataset": "snapshots_ml_flat",
    }
    assert payload["outputs"]["artifacts_root"] == "../../artifacts/research"
    assert payload["windows"]["research_train"] == {"start": "2020-08-03", "end": "2024-04-30"}
    assert payload["windows"]["research_valid"] == {"start": "2024-05-01", "end": "2024-07-31"}
    assert payload["windows"]["full_model"] == defaults["windows"]["full_model"]
    assert payload["windows"]["final_holdout"] == {"start": "2024-08-01", "end": "2024-10-31"}
    assert payload["training"]["cv_config"] == defaults["training"]["cv_config"]
    assert payload["policy"]["stage1"]["threshold_grid"] == defaults["policy"]["stage1"]["threshold_grid"]
    assert payload["runtime"]["prefilter_gate_ids"] == defaults["runtime"]["prefilter_gate_ids"]
    assert defaults["runtime"]["block_expiry"] is False
