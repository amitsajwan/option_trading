"""Tests for dual direction bundle loading and resolution in ml_entry."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_sub_bundle(win_prob: float) -> dict:
    """Make a minimal direction_only_bundle sub-bundle for testing."""
    import numpy as np

    model = MagicMock()
    model.predict_proba.return_value = np.array([[1 - win_prob, win_prob]])
    return {
        "kind": "direction_only_bundle",
        "model": model,
        "features": ["vix_current", "ctx_is_high_vix_day"],
        "feature_medians": {"vix_current": 15.0, "ctx_is_high_vix_day": 0.0},
        "label_map": {"CE": 1, "PE": 0},
    }


def _make_dual_bundle(ce_win_prob: float, pe_win_prob: float) -> dict:
    return {
        "kind": "direction_dual_bundle",
        "ce_bundle": _make_sub_bundle(ce_win_prob),
        "pe_bundle": _make_sub_bundle(pe_win_prob),
    }


def _make_snap(vix: float = 14.0) -> MagicMock:
    snap = MagicMock()
    snap.velocity_features = {}
    snap.raw_payload = {"vix_current": vix, "ctx_is_high_vix_day": 0}
    snap.fut_return_5m = 0.001
    return snap


class TestResolveDual:
    def test_ce_wins_when_higher(self):
        from strategy_app.engines.strategies.ml_entry import _resolve_direction_dual
        from strategy_app.contracts import Direction

        bundle = _make_dual_bundle(ce_win_prob=0.65, pe_win_prob=0.55)
        snap = _make_snap()
        with patch("strategy_app.ml.bundle_inference.build_feature_row") as mock_features:
            mock_features.return_value = {"vix_current": 14.0, "ctx_is_high_vix_day": 0.0}
            result = _resolve_direction_dual(bundle, snap)
        assert result == Direction.CE

    def test_pe_wins_when_higher(self):
        from strategy_app.engines.strategies.ml_entry import _resolve_direction_dual
        from strategy_app.contracts import Direction

        bundle = _make_dual_bundle(ce_win_prob=0.52, pe_win_prob=0.72)
        snap = _make_snap()
        with patch("strategy_app.ml.bundle_inference.build_feature_row") as mock_features:
            mock_features.return_value = {"vix_current": 14.0, "ctx_is_high_vix_day": 0.0}
            result = _resolve_direction_dual(bundle, snap)
        assert result == Direction.PE

    def test_argmax_when_both_below_50_default(self):
        from strategy_app.engines.strategies.ml_entry import _resolve_direction_dual
        from strategy_app.contracts import Direction

        bundle = _make_dual_bundle(ce_win_prob=0.45, pe_win_prob=0.48)
        snap = _make_snap()
        with patch("strategy_app.ml.bundle_inference.build_feature_row") as mock_features:
            mock_features.return_value = {"vix_current": 14.0, "ctx_is_high_vix_day": 0.0}
            result = _resolve_direction_dual(bundle, snap)
        assert result == Direction.PE

    def test_returns_none_when_both_below_50_strict(self, monkeypatch):
        from strategy_app.engines.strategies.ml_entry import _resolve_direction_dual

        monkeypatch.setenv("DIRECTION_DUAL_MIN_PROB", "0.5")
        bundle = _make_dual_bundle(ce_win_prob=0.45, pe_win_prob=0.48)
        snap = _make_snap()
        with patch("strategy_app.ml.bundle_inference.build_feature_row") as mock_features:
            mock_features.return_value = {"vix_current": 14.0, "ctx_is_high_vix_day": 0.0}
            result = _resolve_direction_dual(bundle, snap)
        assert result is None

    def test_returns_none_on_empty_bundles(self):
        from strategy_app.engines.strategies.ml_entry import _resolve_direction_dual

        bundle = {"kind": "direction_dual_bundle", "ce_bundle": None, "pe_bundle": None}
        snap = _make_snap()
        result = _resolve_direction_dual(bundle, snap)
        assert result is None


class TestLoadDirBundle:
    def test_accepts_dual_bundle_kind(self, tmp_path):
        import joblib
        from strategy_app.engines.strategies.ml_entry import _load_dir_bundle

        bundle = {"kind": "direction_dual_bundle", "ce_bundle": {}, "pe_bundle": {}}
        path = tmp_path / "dual.joblib"
        joblib.dump(bundle, path)
        loaded = _load_dir_bundle(str(path))
        assert loaded is not None
        assert loaded["kind"] == "direction_dual_bundle"

    def test_accepts_single_bundle_kind(self, tmp_path):
        import joblib
        from strategy_app.engines.strategies.ml_entry import _load_dir_bundle

        bundle = {"kind": "direction_only_bundle", "model": None, "features": []}
        path = tmp_path / "single.joblib"
        joblib.dump(bundle, path)
        loaded = _load_dir_bundle(str(path))
        assert loaded is not None
        assert loaded["kind"] == "direction_only_bundle"

    def test_rejects_unknown_kind(self, tmp_path):
        import joblib
        from strategy_app.engines.strategies.ml_entry import _load_dir_bundle

        bundle = {"kind": "unknown_bundle"}
        path = tmp_path / "unknown.joblib"
        joblib.dump(bundle, path)
        loaded = _load_dir_bundle(str(path))
        assert loaded is None

    def test_returns_none_on_missing_file(self):
        from strategy_app.engines.strategies.ml_entry import _load_dir_bundle
        result = _load_dir_bundle("/nonexistent/path/model.joblib")
        assert result is None
