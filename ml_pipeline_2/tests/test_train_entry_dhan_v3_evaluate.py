"""Regression test for train_entry_dhan_v3.evaluate()'s threshold grid --
found 2026-09-10 that every one of a real 6-candidate study's own
auto-picked thresholds landed exactly at the old 0.70 ceiling, meaning
the grid was silently truncating the search rather than 0.70 being
genuinely optimal for all six."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_entry_dhan_v3 import DEFAULT_SEPARATION_THRESHOLDS, evaluate


class _FakeModel:
    """predict_proba returning a fixed, known probability array -- lets
    the separation table be computed against exact, hand-checkable values."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = np.asarray(probs, dtype=float)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return np.column_stack([1.0 - self._probs, self._probs])


class TestEvaluateThresholdGrid:
    def test_default_grid_now_extends_past_the_old_070_ceiling(self) -> None:
        assert max(DEFAULT_SEPARATION_THRESHOLDS) > 0.70
        assert 0.75 in DEFAULT_SEPARATION_THRESHOLDS
        assert 0.95 in DEFAULT_SEPARATION_THRESHOLDS

    def test_separation_table_includes_rows_above_070_by_default(self) -> None:
        probs = [0.1] * 5 + [0.9] * 5
        y = np.array([0] * 5 + [1] * 5, dtype=float)
        model = _FakeModel(probs)
        result = evaluate(model, pd.DataFrame(index=range(10)), y, label="test")
        tested_thresholds = {row["thr"] for row in result["separation_table"]}
        assert 0.85 in tested_thresholds
        assert 0.90 in tested_thresholds

    def test_explicit_thresholds_override_the_default_grid(self) -> None:
        probs = [0.2, 0.8]
        y = np.array([0.0, 1.0])
        model = _FakeModel(probs)
        result = evaluate(model, pd.DataFrame(index=range(2)), y, thresholds=[0.5])
        assert [row["thr"] for row in result["separation_table"]] == [0.5]

    def test_a_genuinely_better_threshold_above_070_is_now_discoverable(self) -> None:
        # Three probability tiers: at thr=0.70 both the noisy-mid and the
        # clean-high tier fire together (diluted precision); at thr=0.85
        # only the clean-high tier fires -- a genuinely better, more
        # selective operating point that the old 0.70-ceiling grid could
        # never have found.
        probs = [0.60] * 4 + [0.75] * 4 + [0.90] * 4
        y = np.array([0, 1, 0, 1] + [0, 1, 1, 1] + [1, 1, 1, 1], dtype=float)
        model = _FakeModel(probs)
        result = evaluate(model, pd.DataFrame(index=range(12)), y)
        row_070 = next(r for r in result["separation_table"] if r["thr"] == 0.70)
        row_085 = next(r for r in result["separation_table"] if r["thr"] == 0.85)
        assert row_070["fire_rate"] == pytest.approx(8 / 12, abs=1e-3)
        assert row_085["fire_rate"] == pytest.approx(4 / 12, abs=1e-3)
        assert row_085["precision_fired"] > row_070["precision_fired"]
        assert row_085["precision_fired"] == pytest.approx(1.0)
