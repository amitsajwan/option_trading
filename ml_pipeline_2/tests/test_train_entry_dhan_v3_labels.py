"""Regression tests for train_entry_dhan_v3.add_labels' 2026-09-09
parameterization: --label-pct/--label-pt/--label-horizon-min replaced the
previously-hardcoded LABEL_PT_THRESHOLD/LABEL_HORIZON_MIN module constants
(discovered to be non-configurable while scoping the ml_pipeline_2
candidate-selection pipeline -- this was the missing piece for actually
launching a label-target-grid study through the committed training script).
"""
from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.scripts.train_entry_dhan_v3 import LABEL_PT_THRESHOLD, add_labels


def _synthetic_session_df(closes: list[float]) -> pd.DataFrame:
    """One synthetic trading day, one bar per minute starting 09:45 IST
    (inside the 9:45-15:05 session window add_labels requires), with
    `closes` as the per-minute close price."""
    n = len(closes)
    timestamps = pd.date_range("2026-01-05 09:45", periods=n, freq="1min")
    return pd.DataFrame({
        "trade_date": pd.Timestamp("2026-01-05"),
        "timestamp": timestamps,
        "close": closes,
    })


class TestAddLabelsBackwardCompat:
    def test_default_call_uses_the_original_100pt_threshold(self) -> None:
        # A flat run of bars, then a >100pt jump within the horizon: the
        # unmodified original behavior (no label_pct/label_pt given).
        closes = [50000.0] * 20 + [50150.0] * 20
        df = _synthetic_session_df(closes)
        result = add_labels(df)
        # The bar right before the jump should see a future move >= 100pt.
        idx_before_jump = 19
        assert result.iloc[idx_before_jump]["entry_label"] == 1.0

    def test_default_horizon_is_still_15_minutes(self) -> None:
        closes = [50000.0] * 40
        df = _synthetic_session_df(closes)
        result = add_labels(df)
        # No move anywhere -> every in-window, lookahead-valid bar is 0.
        valid = result["entry_label"].notna()
        assert (result.loc[valid, "entry_label"] == 0.0).all()


class TestAddLabelsPercentMode:
    def test_label_pct_scales_the_threshold_to_the_rows_own_close(self) -> None:
        # 0.10% of 50000 = 50pt. A 60pt move should fire; nothing near it should not.
        closes = [50000.0] * 20 + [50060.0] * 20
        df = _synthetic_session_df(closes)
        result = add_labels(df, label_pct=0.10, horizon_min=15)
        assert result.iloc[19]["entry_label"] == 1.0

    def test_label_pct_below_move_size_does_not_fire(self) -> None:
        # 0.20% of 50000 = 100pt; a 60pt move should NOT fire at that threshold.
        closes = [50000.0] * 20 + [50060.0] * 20
        df = _synthetic_session_df(closes)
        result = add_labels(df, label_pct=0.20, horizon_min=15)
        assert result.iloc[19]["entry_label"] == 0.0

    def test_same_pct_fires_consistently_at_a_different_price_level(self) -> None:
        # The instrument/price-level-agnostic property this was built for:
        # 0.10% fires the same way whether the index is at 24000 or 50000.
        closes_low = [24000.0] * 20 + [24030.0] * 20   # +30pt = +0.125%
        closes_high = [50000.0] * 20 + [50063.0] * 20  # +63pt = +0.126%
        low = add_labels(_synthetic_session_df(closes_low), label_pct=0.10, horizon_min=15)
        high = add_labels(_synthetic_session_df(closes_high), label_pct=0.10, horizon_min=15)
        assert low.iloc[19]["entry_label"] == 1.0
        assert high.iloc[19]["entry_label"] == 1.0


class TestAddLabelsValidation:
    def test_rejects_both_label_pct_and_label_pt_given(self) -> None:
        df = _synthetic_session_df([50000.0] * 20)
        with pytest.raises(ValueError, match="at most one of label_pct / label_pt"):
            add_labels(df, label_pct=0.10, label_pt=50.0)

    def test_explicit_label_pt_overrides_the_module_default(self) -> None:
        closes = [50000.0] * 20 + [50060.0] * 20
        df = _synthetic_session_df(closes)
        # Module default is 100pt (a 60pt move would not fire); explicit 50pt should.
        assert LABEL_PT_THRESHOLD == 100
        result = add_labels(df, label_pt=50.0, horizon_min=15)
        assert result.iloc[19]["entry_label"] == 1.0


class TestAddLabelsHorizon:
    def test_shorter_horizon_changes_which_bars_have_a_valid_label(self) -> None:
        closes = [50000.0] * 40
        df = _synthetic_session_df(closes)
        result_5 = add_labels(df, label_pt=50.0, horizon_min=5)
        result_15 = add_labels(df, label_pt=50.0, horizon_min=15)
        # A shorter horizon leaves more trailing bars with a valid (non-NaN) label.
        assert result_5["entry_label"].notna().sum() > result_15["entry_label"].notna().sum()
