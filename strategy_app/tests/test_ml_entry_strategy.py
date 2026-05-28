from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from strategy_app.contracts import Direction, SignalType
from strategy_app.engines.strategies.ml_entry import MlEntryStrategy
from strategy_app.market.snapshot_accessor import SnapshotAccessor


def _minimal_snapshot_payload() -> dict:
    return {
        "snapshot_id": "snap-1",
        "timestamp": "2024-08-15T06:00:00+00:00",
        "trade_date": "2024-08-15",
        "atm_strike": 52000,
        "atm_ce_close": 120.0,
        "atm_pe_close": 115.0,
        "fut_return_5m": 0.002,
    }


def test_ml_entry_returns_none_without_model_path(monkeypatch) -> None:
    monkeypatch.delenv("ENTRY_ML_MODEL_PATH", raising=False)
    strategy = MlEntryStrategy()
    vote = strategy.evaluate(_minimal_snapshot_payload(), None, MagicMock())
    assert vote is None


def test_ml_entry_emits_vote_when_prob_above_threshold(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_ML_MIN_PROB", "0.50")
    monkeypatch.setenv("ML_ENTRY_DIRECTION_MODE", "momentum")
    bundle = {
        "kind": "entry_only_bundle",
        "features": ["fut_return_5m"],
        "feature_medians": {"fut_return_5m": 0.0},
        "model": MagicMock(),
    }
    bundle["model"].predict_proba.return_value = [[0.4, 0.72]]

    strategy = MlEntryStrategy()
    with patch(
        "strategy_app.engines.strategies.ml_entry.load_joblib_bundle",
        return_value=bundle,
    ):
        with patch(
            "strategy_app.engines.strategies.ml_entry.predict_positive_class_prob",
            return_value=0.72,
        ):
            with patch.dict(os.environ, {"ENTRY_ML_MODEL_PATH": "/fake/entry.joblib"}):
                vote = strategy.evaluate(_minimal_snapshot_payload(), None, MagicMock())

    assert vote is not None
    assert vote.strategy_name == "ML_ENTRY"
    assert vote.signal_type == SignalType.ENTRY
    assert vote.direction == Direction.CE
    assert vote.confidence >= 0.72


def test_ml_entry_pe_only_forces_pe(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_ML_MIN_PROB", "0.50")
    monkeypatch.setenv("ML_ENTRY_PE_ONLY", "1")
    bundle = {
        "kind": "entry_only_bundle",
        "features": ["fut_return_5m"],
        "feature_medians": {"fut_return_5m": 0.0},
        "model": MagicMock(),
    }
    strategy = MlEntryStrategy()
    snap = _minimal_snapshot_payload()
    snap["fut_return_5m"] = -0.01
    with patch(
        "strategy_app.engines.strategies.ml_entry.load_joblib_bundle",
        return_value=bundle,
    ):
        with patch(
            "strategy_app.engines.strategies.ml_entry.predict_positive_class_prob",
            return_value=0.72,
        ):
            with patch.dict(os.environ, {"ENTRY_ML_MODEL_PATH": "/fake/entry.joblib"}):
                vote = strategy.evaluate(snap, None, MagicMock())
    assert vote is not None
    assert vote.direction == Direction.PE
    assert vote.raw_signals.get("direction_source") == "pe_only"


def test_ml_entry_block_ce_skips_ce_momentum(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_ML_MIN_PROB", "0.50")
    monkeypatch.setenv("ML_ENTRY_DIRECTION_MODE", "momentum")
    monkeypatch.setenv("ML_ENTRY_BLOCK_CE", "1")
    bundle = {
        "kind": "entry_only_bundle",
        "features": ["fut_return_5m"],
        "feature_medians": {"fut_return_5m": 0.0},
        "model": MagicMock(),
    }
    strategy = MlEntryStrategy()
    with patch(
        "strategy_app.engines.strategies.ml_entry.load_joblib_bundle",
        return_value=bundle,
    ):
        with patch(
            "strategy_app.engines.strategies.ml_entry.predict_positive_class_prob",
            return_value=0.72,
        ):
            with patch.dict(os.environ, {"ENTRY_ML_MODEL_PATH": "/fake/entry.joblib"}):
                vote = strategy.evaluate(_minimal_snapshot_payload(), None, MagicMock())
    assert vote is None


def test_ml_entry_block_pe_skips_pe_momentum(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_ML_MIN_PROB", "0.50")
    monkeypatch.setenv("ML_ENTRY_DIRECTION_MODE", "momentum")
    monkeypatch.setenv("ML_ENTRY_BLOCK_PE", "1")
    bundle = {
        "kind": "entry_only_bundle",
        "features": ["fut_return_5m"],
        "feature_medians": {"fut_return_5m": 0.0},
        "model": MagicMock(),
    }
    strategy = MlEntryStrategy()
    snap = _minimal_snapshot_payload()
    snap["futures_derived"] = {"fut_return_5m": -0.01}  # momentum points PE
    with patch(
        "strategy_app.engines.strategies.ml_entry.load_joblib_bundle",
        return_value=bundle,
    ):
        with patch(
            "strategy_app.engines.strategies.ml_entry.predict_positive_class_prob",
            return_value=0.72,
        ):
            with patch.dict(os.environ, {"ENTRY_ML_MODEL_PATH": "/fake/entry.joblib"}):
                vote = strategy.evaluate(snap, None, MagicMock())
    assert vote is None


def test_ml_entry_ce_only_forces_ce(monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_ML_MIN_PROB", "0.50")
    monkeypatch.setenv("ML_ENTRY_CE_ONLY", "1")
    bundle = {
        "kind": "entry_only_bundle",
        "features": ["fut_return_5m"],
        "feature_medians": {"fut_return_5m": 0.0},
        "model": MagicMock(),
    }
    strategy = MlEntryStrategy()
    snap = _minimal_snapshot_payload()
    snap["futures_derived"] = {"fut_return_5m": -0.01}  # momentum would say PE; CE_ONLY must override
    with patch(
        "strategy_app.engines.strategies.ml_entry.load_joblib_bundle",
        return_value=bundle,
    ):
        with patch(
            "strategy_app.engines.strategies.ml_entry.predict_positive_class_prob",
            return_value=0.72,
        ):
            with patch.dict(os.environ, {"ENTRY_ML_MODEL_PATH": "/fake/entry.joblib"}):
                vote = strategy.evaluate(snap, None, MagicMock())
    assert vote is not None
    assert vote.direction == Direction.CE
    assert vote.raw_signals.get("direction_source") == "ce_only"
