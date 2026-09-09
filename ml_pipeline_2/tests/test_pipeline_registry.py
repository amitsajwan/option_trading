"""Unit tests for the pure parts of ml_pipeline_2.pipeline.registry
(document construction) -- the Mongo-touching RunRegistry class itself is
exercised on the VM, not here, matching this repo's existing convention
for Mongo-backed code."""
from __future__ import annotations

from datetime import datetime

from ml_pipeline_2.pipeline.registry import COLLECTION_NAME, build_run_document


def test_build_run_document_shape() -> None:
    doc = build_run_document(
        instrument="nifty",  # lowercase in -> uppercase out, same normalization as instrument_config
        study_id="label_target_2026-09-09",
        node="train",
        config={"label_pct": 0.0010, "horizon_min": 15},
        result={"holdout_auc": 0.6793, "ship_gates": {"auc>=0.70": False}},
        verdict="usable",
        artifact_path="/tmp/nifty_label_grid/nifty_015pct_v3_010pct.joblib",
    )
    assert doc["instrument"] == "NIFTY"
    assert doc["study_id"] == "label_target_2026-09-09"
    assert doc["node"] == "train"
    assert doc["config"]["label_pct"] == 0.0010
    assert doc["result"]["holdout_auc"] == 0.6793
    assert doc["verdict"] == "usable"
    assert isinstance(doc["created_at"], datetime)


def test_build_run_document_defaults() -> None:
    doc = build_run_document(
        instrument="BANKNIFTY", study_id="s1", node="backtest",
        config={}, result={},
    )
    assert doc["verdict"] == "pending"
    assert doc["artifact_path"] is None
    assert doc["notes"] == ""


def test_collection_name_is_stable() -> None:
    # Regression guard: renaming this collection silently would orphan
    # every previously-recorded run without an obvious error.
    assert COLLECTION_NAME == "ml_pipeline_runs"
