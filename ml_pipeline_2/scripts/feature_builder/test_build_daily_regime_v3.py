from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.scripts.feature_builder.regime_daily import (
    REGIME_COLUMNS,
    TRADING_DAYS_PER_YEAR,
    RV20_MIN_PERIODS,
    VIX_HIGH_THRESHOLD,
    compute_regime_features,
    compute_vix_regime_features,
    merge_regime_tables,
)


def test_regime_rv20_uses_prior_day_only():
    n = 80
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    close = 100.0 * np.cumprod(1.0 + np.random.default_rng(42).normal(0, 0.005, n))
    daily = pd.DataFrame({"trade_date": dates, "close": close}).sort_values("trade_date")

    out = compute_regime_features(daily)
    ret = daily["close"].pct_change()
    rv_unshifted = ret.rolling(RV20_MIN_PERIODS, min_periods=RV20_MIN_PERIODS).std() * np.sqrt(
        TRADING_DAYS_PER_YEAR
    )

    checked = 0
    for i in range(1, len(daily)):
        td = daily.iloc[i]["trade_date"]
        actual = out.loc[out["trade_date"] == td, "regime_rv20"].iloc[0]
        expected = rv_unshifted.iloc[i - 1]
        if pd.isna(actual) or pd.isna(expected):
            continue
        assert actual == pytest.approx(float(expected), rel=1e-9)
        checked += 1
    assert checked >= 5


def test_regime_columns_present():
    daily = pd.DataFrame(
        {
            "trade_date": pd.date_range("2023-01-02", periods=70, freq="B"),
            "close": np.linspace(40000, 45000, 70),
        }
    )
    out = compute_regime_features(daily)
    assert list(out.columns) == ["trade_date"] + REGIME_COLUMNS
    assert out["regime_sma20_slope"].notna().sum() >= 1


def test_empty_daily():
    out = compute_regime_features(pd.DataFrame(columns=["trade_date", "close"]))
    assert out.empty


def test_vix_regime_prior_day():
    dates = pd.date_range("2024-01-02", periods=5, freq="B")
    vix = pd.DataFrame({"trade_date": dates, "vix_close": [14.0, 21.0, 15.0, 19.0, 12.0]})
    out = compute_vix_regime_features(vix)
    assert out.iloc[2]["regime_vix_close"] == pytest.approx(21.0)
    assert out.iloc[2]["regime_vix_high"] == 1.0
    assert out.iloc[1]["regime_vix_high"] == 0.0


def test_merge_regime_tables():
    dates = pd.date_range("2024-01-02", periods=30, freq="B")
    price = compute_regime_features(
        pd.DataFrame({"trade_date": dates, "close": np.linspace(100, 110, 30)})
    )
    vix = compute_vix_regime_features(
        pd.DataFrame({"trade_date": dates, "vix_close": np.full(30, VIX_HIGH_THRESHOLD - 1)})
    )
    merged = merge_regime_tables(price, vix)
    assert "regime_rv20" in merged.columns
    assert "regime_vix_close" in merged.columns
