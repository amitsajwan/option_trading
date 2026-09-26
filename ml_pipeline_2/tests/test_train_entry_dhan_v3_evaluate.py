"""Regression test for train_entry_dhan_v3.evaluate()'s threshold grid --
found 2026-09-10 that every one of a real 6-candidate study's own
auto-picked thresholds landed exactly at the old 0.70 ceiling, meaning
the grid was silently truncating the search rather than 0.70 being
genuinely optimal for all six."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_entry_dhan_v3 import (
    DEFAULT_SEPARATION_THRESHOLDS,
    compute_ship_gates,
    evaluate,
)


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


class TestComputeShipGates:
    """2026-09-26: extracted from main()'s inline dict and standardized on
    docs/MODEL_STUDY_CHECKLIST.md's bar (auc>=0.70, separation>=0.15,
    ece<=0.05, prob_spread>=0.20) -- the previous inline gates
    (auc>=0.62, separation>=0.08, no ece check) are exactly what let
    nifty_entry_010pct_v1.joblib (holdout AUC 0.667) ship over its own
    AUC-0.7327 sibling."""

    _GOOD_SEPARATION_TABLE = [{"thr": 0.5, "separation": 0.20, "degenerate": False}]

    def test_all_pass_when_every_metric_clears_the_checklist_bar(self) -> None:
        gates = compute_ship_gates(
            auc=0.75, ece=0.03, separation_table=self._GOOD_SEPARATION_TABLE, prob_spread=0.25,
        )
        assert all(gates.values())

    def test_the_actual_010pct_v1_auc_would_now_fail(self) -> None:
        # The real deployed bundle's holdout AUC -- would have shipped under
        # the old auc>=0.62 gate, must fail under the new auc>=0.70 one.
        gates = compute_ship_gates(
            auc=0.667, ece=0.03, separation_table=self._GOOD_SEPARATION_TABLE, prob_spread=0.25,
        )
        assert gates["auc>=0.7"] is False
        assert not all(gates.values())

    def test_missing_ece_fails_rather_than_silently_passing(self) -> None:
        gates = compute_ship_gates(
            auc=0.80, ece=None, separation_table=self._GOOD_SEPARATION_TABLE, prob_spread=0.25,
        )
        assert gates["ece<=0.05"] is False

    def test_degenerate_rows_are_excluded_from_the_separation_check(self) -> None:
        table = [{"thr": 0.5, "separation": 0.99, "degenerate": True}]
        gates = compute_ship_gates(auc=0.80, ece=0.02, separation_table=table, prob_spread=0.25)
        assert gates["separation>=0.15"] is False

    def test_thresholds_are_overridable(self) -> None:
        gates = compute_ship_gates(
            auc=0.65, ece=0.03, separation_table=self._GOOD_SEPARATION_TABLE, prob_spread=0.25,
            auc_min=0.60,
        )
        assert gates["auc>=0.6"] is True
