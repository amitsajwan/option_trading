from __future__ import annotations

from ml_pipeline_2.catalog import model_names, model_specs_by_name


def test_model_catalog_includes_stronger_tree_presets() -> None:
    specs = model_specs_by_name()

    assert {
        "xgb_deep_v1",
        "xgb_deep_slow_v1",
        "lgbm_large_v1",
        "lgbm_large_dart_v1",
    }.issubset(specs)
    assert "xgb_balanced" in model_names()
    assert "xgb_regularized" in model_names()
    assert "lgbm_fast" in model_names()

    assert specs["xgb_deep_v1"].params["max_depth"] == 6
    assert specs["xgb_deep_v1"].params["n_estimators"] == 500
    assert specs["xgb_deep_slow_v1"].params["learning_rate"] == 0.015
    assert specs["lgbm_large_v1"].params["num_leaves"] == 63
    assert specs["lgbm_large_v1"].params["max_depth"] == 8
    assert specs["lgbm_large_dart_v1"].params["boosting_type"] == "dart"
