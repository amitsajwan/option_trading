"""Tests for ml_pipeline_2.scripts.study_direction_trust_meta -- the
direction-call-trust meta-labeling study orchestration. Covers the pure/
synthetic-data-testable pieces (window splitting, thin-sample guards, the
delayed-stress-test aggregation) with fixtures shaped like
`direction_trust_meta.compute_composite_calls`' real output; the real
empirical walk-forward run itself (real NIFTY data) is exercised separately
by actually running the study (see the task report), not by this test file.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.conditional_direction import (
    REQUIRED_RAW_COLUMNS,
    WalkForwardWindow,
    build_expanding_windows,
)
from ml_pipeline_2.pipeline.direction_trust_meta import (
    CE,
    PE,
    add_bars_since_active,
    compute_composite_calls,
    compute_composite_correct,
)
from ml_pipeline_2.pipeline.meta_direction_features import (
    BARS_SINCE_ACTIVE_COL,
    SELECTED_REGIME_FEATURES,
    SELECTED_VELOCITY_FEATURES,
)
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3
from ml_pipeline_2.scripts.study_direction_trust_meta import (
    evaluate_window,
    run_delayed_stress_test,
    select_window_frames,
)


def _synthetic_full_frame(n_days: int, bars_per_day: int, *, seed: int = 0) -> pd.DataFrame:
    """A frame shaped like the real joined NIFTY training view (raw resolver
    columns + payoff_winning_side + ENTRY_FEATURES_V3 + velocity/regime
    columns), fully synthetic, with a REAL (not planted) relationship
    between fut_return_5m and payoff_winning_side so composite has genuine,
    if noisy, unconditional skill to fire on -- close to this project's
    real ~50% finding, not a degenerate constant."""
    rng = np.random.default_rng(seed)
    n = n_days * bars_per_day
    dates = np.repeat([f"2026-01-{d:02d}" for d in range(1, n_days + 1)], bars_per_day)
    bar_index = np.tile(np.arange(bars_per_day), n_days)

    df = pd.DataFrame({col: rng.normal(size=n) for col in REQUIRED_RAW_COLUMNS if col not in ("orh_broken", "orl_broken")})
    df["orh_broken"] = False
    df["orl_broken"] = False
    df["trade_date"] = dates
    df["bar_index"] = bar_index
    df["payoff_winning_side"] = np.where(rng.random(n) < 0.5, CE, PE)
    df["payoff_score"] = rng.normal(scale=0.02, size=n)
    for col in ENTRY_FEATURES_V3:
        if col not in df.columns:
            df[col] = rng.normal(size=n)
    for col in list(SELECTED_VELOCITY_FEATURES) + list(SELECTED_REGIME_FEATURES):
        if col not in df.columns:
            df[col] = rng.normal(size=n)
    return df


def _prepared_frame(n_days: int = 40, bars_per_day: int = 30, seed: int = 0) -> pd.DataFrame:
    df = _synthetic_full_frame(n_days, bars_per_day, seed=seed)
    df = compute_composite_calls(df)
    df["composite_correct"] = compute_composite_correct(df["composite_direction"], df["payoff_winning_side"])
    df[BARS_SINCE_ACTIVE_COL] = add_bars_since_active(df).to_numpy()
    return df


class TestSelectWindowFrames:
    def test_splits_by_date_range_without_overlap(self) -> None:
        df = _prepared_frame(n_days=10, bars_per_day=5)
        window = WalkForwardWindow(
            window_id=1, train_start="2026-01-01", train_end="2026-01-05",
            test_start="2026-01-06", test_end="2026-01-10",
        )
        train_df, test_df = select_window_frames(df, window)
        assert train_df["trade_date"].max() <= "2026-01-05"
        assert test_df["trade_date"].min() >= "2026-01-06"
        assert len(train_df) + len(test_df) == len(df)


class TestEvaluateWindow:
    xgb = pytest.importorskip("xgboost")

    def test_reports_ok_status_with_expected_keys_on_a_real_sized_window(self) -> None:
        df = _prepared_frame(n_days=60, bars_per_day=40, seed=1)
        dates = sorted(df["trade_date"].unique().tolist())
        windows = build_expanding_windows(dates, n_windows=2, min_train_days=30)
        result = evaluate_window(df, windows[0])
        assert result["status"] == "ok"
        assert "meta_auc_test" in result
        assert "trust_filters" in result
        assert len(result["trust_filters"]) == 3
        assert "_test_meta" in result  # stashed for pooling, dropped later by _json_safe

    def test_thin_sample_reports_status_without_fitting(self) -> None:
        df = _prepared_frame(n_days=6, bars_per_day=5, seed=2)
        window = WalkForwardWindow(
            window_id=1, train_start="2026-01-01", train_end="2026-01-03",
            test_start="2026-01-04", test_end="2026-01-06",
        )
        result = evaluate_window(df, window)
        assert result["status"] == "thin_sample_or_degenerate_train"
        assert "baseline_accuracy_test" in result

    def test_baseline_accuracy_is_near_chance_on_pure_noise(self) -> None:
        # With REQUIRED_RAW_COLUMNS drawn independently of payoff_winning_side,
        # composite's accuracy on fired bars should sit near 50% -- a coarse
        # sanity check that evaluate_window isn't silently leaking the label.
        df = _prepared_frame(n_days=60, bars_per_day=40, seed=3)
        dates = sorted(df["trade_date"].unique().tolist())
        windows = build_expanding_windows(dates, n_windows=2, min_train_days=30)
        result = evaluate_window(df, windows[0])
        assert 0.35 < result["baseline_accuracy_test"] < 0.65


class TestRunDelayedStressTest:
    def test_no_ok_windows_reports_skipped(self) -> None:
        out = run_delayed_stress_test(
            [{"status": "thin_sample_or_degenerate_train", "window": 1}],
            "unused_parquet_base", horizon_bars=15, lot_size=65,
        )
        assert out["status"] == "skipped_no_ok_windows"

    def test_pools_across_windows_and_reports_per_frac(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fake recompute_composite_correct_at_delay so this test needs no
        # real parquet data -- only the pooling/aggregation logic is under
        # test here (the recompute primitive itself has its own tests in
        # test_direction_trust_meta.py).
        import ml_pipeline_2.scripts.study_direction_trust_meta as mod

        def fake_recompute(rows, parquet_base, *, delay_bars, horizon_bars, lot_size):
            # Everything stays correct at every delay -- a clean "survives"
            # case, easy to assert on.
            return pd.Series(1.0, index=rows.index)

        monkeypatch.setattr(mod, "recompute_composite_correct_at_delay", fake_recompute)

        n = 100
        window_results = []
        for w in (1, 2):
            test_meta = pd.DataFrame(
                {
                    "window": w,
                    "trade_date": [f"2026-0{w}-01"] * n,
                    "bar_index": range(n),
                    "composite_direction": [CE] * n,
                    "composite_correct": np.where(np.arange(n) < 80, 1.0, 0.0),  # 80% baseline
                    "meta_score": np.linspace(0, 1, n),
                }
            )
            window_results.append({"status": "ok", "window": w, "_test_meta": test_meta})

        out = run_delayed_stress_test(window_results, "unused", horizon_bars=15, lot_size=65)
        assert out["status"] == "ok"
        assert out["n_pooled_test_fired"] == 2 * n
        assert "0.25" in out["trust_filters"]
        for frac_result in out["trust_filters"].values():
            assert frac_result["status"] in {"survived", "collapsed", "no_edge_at_fire", "thin_sample"}
