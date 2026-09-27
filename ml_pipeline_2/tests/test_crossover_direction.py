"""Tests for ml_pipeline_2.pipeline.crossover_direction -- hand-constructed
series where the expected crossover/level-cross/confluence event is known
exactly (including the precise bar, not off-by-one), plus day-boundary-
bleed regression tests (a signal must never treat yesterday's last bar as
"previous" for today's first bar)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.crossover_direction import (
    CE,
    PE,
    adx_di_cross_vote,
    bollinger_breakout_vote,
    build_full_day_frame,
    compute_bollinger_bands,
    compute_di_and_adx,
    compute_macd,
    compute_rsi_14,
    compute_supertrend,
    confluence_vote,
    detect_cross,
    ema_cross_vote,
    extract_full_day_row,
    macd_cross_vote,
    pctb_breakout_vote,
    rsi_reversal_vote,
    rsi_trend_cross_vote,
    supertrend_flip_vote,
)


# ── Fake snapshot stub (mirrors the handful of SnapshotAccessor attributes
# extract_full_day_row reads -- no dependency on real parquet data) ────────

class _FakeSnap:
    def __init__(self, trade_date, timestamp, close, high, low, ema9, ema21, ema50, mtf_derived=None):
        self.trade_date = trade_date
        self._timestamp = timestamp
        self.fut_close = close
        self.fut_high = high
        self.fut_low = low
        self.ema_9 = ema9
        self.ema_21 = ema21
        self.ema_50 = ema50
        self._mtf_derived = mtf_derived

    @property
    def raw_payload(self):
        payload = {"timestamp": self._timestamp}
        if self._mtf_derived is not None:
            payload["mtf_derived"] = self._mtf_derived
        return payload


class TestFullDayFrameExtraction:
    def test_extract_full_day_row_reads_expected_fields(self) -> None:
        snap = _FakeSnap("2026-01-05", "2026-01-05T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 100.2, 99.8)
        row = extract_full_day_row(snap, bar_index=0)
        assert row == {
            "trade_date": "2026-01-05", "timestamp": "2026-01-05T09:15:00+05:30", "bar_index": 0,
            "fut_close": 100.0, "fut_high": 101.0, "fut_low": 99.0,
            "ema_9": 100.5, "ema_21": 100.2, "ema_50": 99.8,
            "mtf_rsi_14_5m": None, "mtf_ema_9_5m": None, "mtf_ema_21_5m": None,
            "mtf_macd_line_5m": None, "mtf_macd_signal_5m": None, "mtf_bb_pct_b_5m": None,
        }

    def test_extract_full_day_row_reads_mtf_derived_fields_when_present(self) -> None:
        snap = _FakeSnap(
            "2026-01-05", "t0", 100.0, 101.0, 99.0, 100.5, 100.2, 99.8,
            mtf_derived={
                "rsi_14_5m": 62.3, "ema_9_5m": 101.1, "ema_21_5m": 100.4,
                "macd_line_5m": 1.2, "macd_signal_5m": 0.9, "bb_pct_b_5m": 0.85,
                "ema_50_15m": None,  # present-but-null field, not extracted by this module (see docstring)
            },
        )
        row = extract_full_day_row(snap, bar_index=5)
        assert row["mtf_rsi_14_5m"] == 62.3
        assert row["mtf_ema_9_5m"] == 101.1
        assert row["mtf_ema_21_5m"] == 100.4
        assert row["mtf_macd_line_5m"] == 1.2
        assert row["mtf_macd_signal_5m"] == 0.9
        assert row["mtf_bb_pct_b_5m"] == 0.85

    def test_missing_mtf_derived_block_degrades_to_none_not_a_crash(self) -> None:
        snap = _FakeSnap("2026-01-05", "t0", 100.0, 101.0, 99.0, 100.5, 100.2, 99.8, mtf_derived=None)
        row = extract_full_day_row(snap, bar_index=0)
        assert row["mtf_rsi_14_5m"] is None
        assert row["mtf_macd_line_5m"] is None

    def test_build_full_day_frame_assigns_positional_bar_index(self) -> None:
        snaps = [
            _FakeSnap("2026-01-05", f"t{i}", 100.0 + i, 101.0, 99.0, 100.0, 100.0, 100.0)
            for i in range(3)
        ]
        df = build_full_day_frame(snaps)
        assert list(df["bar_index"]) == [0, 1, 2]
        assert list(df["fut_close"]) == [100.0, 101.0, 102.0]

    def test_empty_snapshot_list_returns_empty_frame_with_expected_columns(self) -> None:
        df = build_full_day_frame([])
        assert len(df) == 0
        assert "fut_close" in df.columns
        assert "mtf_rsi_14_5m" in df.columns


# ── Fresh indicators: per-day reset via the "two identical days must give
# identical indicator values" invariant (proves no cross-day state leak
# without needing to hand-derive EMA arithmetic) ───────────────────────────

def _two_day_frame(day1_close, day2_close) -> pd.DataFrame:
    dates = ["2026-01-05"] * len(day1_close) + ["2026-01-06"] * len(day2_close)
    closes = list(day1_close) + list(day2_close)
    return pd.DataFrame({"trade_date": dates, "bar_index": list(range(len(day1_close))) + list(range(len(day2_close))), "fut_close": closes})


class TestComputeRsi14:
    def test_identical_days_produce_identical_rsi_no_cross_day_leak(self) -> None:
        prices = [100, 101, 99, 102, 104, 103, 105, 108, 106, 110]
        df = _two_day_frame(prices, prices)
        rsi = compute_rsi_14(df)
        day1 = rsi.iloc[:10].to_numpy()
        day2 = rsi.iloc[10:].to_numpy()
        np.testing.assert_allclose(day1, day2)

    def test_a_strict_uptrend_saturates_rsi_near_100(self) -> None:
        prices = list(range(100, 130))
        df = pd.DataFrame({"trade_date": ["2026-01-05"] * len(prices), "bar_index": range(len(prices)), "fut_close": prices})
        rsi = compute_rsi_14(df)
        assert rsi.iloc[-1] == pytest.approx(100.0)

    def test_matches_feature_engine_rsi_directly_on_a_single_day(self) -> None:
        from snapshot_app.core.feature_engine import _rsi as fe_rsi

        prices = [100, 102, 101, 105, 103, 107, 110, 108, 112, 115]
        df = pd.DataFrame({"trade_date": ["2026-01-05"] * len(prices), "bar_index": range(len(prices)), "fut_close": prices})
        rsi = compute_rsi_14(df)
        expected = fe_rsi(pd.Series(prices, dtype=float), 14)
        np.testing.assert_allclose(rsi.to_numpy(), expected.to_numpy(), equal_nan=True)


class TestComputeMacd:
    def test_identical_days_produce_identical_macd_no_cross_day_leak(self) -> None:
        prices = [100, 101, 103, 102, 105, 108, 107, 110, 112, 111, 115, 118, 116, 120, 122]
        df = _two_day_frame(prices, prices)
        out = compute_macd(df)
        for col in ("macd_line", "macd_signal", "macd_hist"):
            day1 = out[col].iloc[: len(prices)].to_numpy()
            day2 = out[col].iloc[len(prices):].to_numpy()
            np.testing.assert_allclose(day1, day2)

    def test_flat_price_series_gives_zero_macd(self) -> None:
        prices = [100.0] * 40
        df = pd.DataFrame({"trade_date": ["2026-01-05"] * len(prices), "bar_index": range(len(prices)), "fut_close": prices})
        out = compute_macd(df)
        np.testing.assert_allclose(out["macd_line"].to_numpy(), 0.0, atol=1e-9)
        np.testing.assert_allclose(out["macd_hist"].to_numpy(), 0.0, atol=1e-9)

    def test_sustained_uptrend_eventually_has_positive_histogram(self) -> None:
        prices = [100 + 0.5 * i for i in range(60)]
        df = pd.DataFrame({"trade_date": ["2026-01-05"] * len(prices), "bar_index": range(len(prices)), "fut_close": prices})
        out = compute_macd(df)
        assert out["macd_hist"].iloc[-1] > 0


# ── Crossover / level-cross primitives: exact-bar detection ────────────────

class TestDetectCross:
    def test_detects_up_and_down_cross_at_the_exact_bar(self) -> None:
        day = pd.Series(["d1"] * 6)
        fast = pd.Series([1.0, 1.0, 3.0, 3.0, 1.0, 1.0])
        slow = pd.Series([2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
        votes = detect_cross(fast, slow, day=day)
        assert list(votes) == [None, None, CE, None, PE, None]

    def test_touching_the_level_without_crossing_does_not_fire(self) -> None:
        day = pd.Series(["d1"] * 3)
        fast = pd.Series([1.0, 2.0, 2.0])  # approaches and sits exactly on slow, never exceeds it
        slow = pd.Series([2.0, 2.0, 2.0])
        votes = detect_cross(fast, slow, day=day)
        assert list(votes) == [None, None, None]

    def test_day_boundary_prevents_false_cross_at_first_bar_of_new_day(self) -> None:
        day = pd.Series(["d1", "d1", "d1", "d2", "d2", "d2"])
        # diff sequence: -1, -1, -5, +5, +5, +5 -- a huge jump exactly at the
        # d1->d2 boundary that WOULD look like an up-cross without day-aware masking.
        fast = pd.Series([1.0, 1.0, -3.0, 7.0, 7.0, 7.0])
        slow = pd.Series([2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
        votes = detect_cross(fast, slow, day=day)
        assert votes.iloc[3] is None  # first bar of d2 -- must not fire despite the huge apparent jump
        assert list(votes) == [None, None, None, None, None, None]

    def test_first_bar_overall_never_fires(self) -> None:
        day = pd.Series(["d1"])
        votes = detect_cross(pd.Series([100.0]), pd.Series([1.0]), day=day)
        assert votes.iloc[0] is None


class TestEmaAndMacdCrossVote:
    def test_ema_cross_vote_wraps_detect_cross_with_named_columns(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1"] * 4,
            "ema_9": [1.0, 3.0, 3.0, 1.0],
            "ema_21": [2.0, 2.0, 2.0, 2.0],
        })
        votes = ema_cross_vote(df, fast_col="ema_9", slow_col="ema_21")
        assert list(votes) == [None, CE, None, PE]

    def test_macd_cross_vote_wraps_detect_cross_with_default_column_names(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1"] * 4,
            "macd_line": [-1.0, 1.0, 1.0, -1.0],
            "macd_signal": [0.0, 0.0, 0.0, 0.0],
        })
        votes = macd_cross_vote(df)
        assert list(votes) == [None, CE, None, PE]


class TestRsiTrendCrossVote:
    def test_crosses_up_and_down_through_50_at_the_exact_bar(self) -> None:
        df = pd.DataFrame({"trade_date": ["d1"] * 6, "rsi_14": [45.0, 48.0, 52.0, 55.0, 50.0, 47.0]})
        votes = rsi_trend_cross_vote(df)
        assert list(votes) == [None, None, CE, None, None, PE]

    def test_custom_level(self) -> None:
        df = pd.DataFrame({"trade_date": ["d1"] * 3, "rsi_14": [55.0, 62.0, 58.0]})
        votes = rsi_trend_cross_vote(df, level=60.0)
        assert list(votes) == [None, CE, PE]


class TestRsiReversalVote:
    def test_bullish_bounce_from_oversold_and_bearish_fade_from_overbought(self) -> None:
        df = pd.DataFrame({"trade_date": ["d1"] * 7, "rsi_14": [25.0, 28.0, 32.0, 75.0, 68.0, 71.0, 65.0]})
        votes = rsi_reversal_vote(df)
        assert list(votes) == [None, None, CE, None, PE, None, PE]

    def test_never_dipping_into_either_zone_never_fires(self) -> None:
        df = pd.DataFrame({"trade_date": ["d1"] * 5, "rsi_14": [45.0, 50.0, 55.0, 48.0, 52.0]})
        votes = rsi_reversal_vote(df)
        assert list(votes) == [None, None, None, None, None]

    def test_day_boundary_prevents_bleed(self) -> None:
        # d1 ends deep oversold; d2 opens just above 30 -- without day-aware
        # masking this would look like a bullish bounce at d2's first bar.
        df = pd.DataFrame({
            "trade_date": ["d1", "d1", "d2", "d2"],
            "rsi_14": [40.0, 20.0, 35.0, 35.0],
        })
        votes = rsi_reversal_vote(df)
        assert votes.iloc[2] is None


# ── Confluence: hard AND-gate across independent signals ───────────────────

class TestConfluenceVote:
    def _day(self, n: int, label: str = "d1") -> pd.Series:
        return pd.Series([label] * n)

    def test_fires_when_two_distinct_signals_agree_within_window(self) -> None:
        day = self._day(6)
        votes = {
            "a": pd.Series([CE, None, None, None, None, None]),
            "b": pd.Series([None, None, CE, None, None, None]),
            "c": pd.Series([None, None, None, None, PE, None]),
        }
        out = confluence_vote(votes, day=day, min_agree=2, window_bars=2)
        assert list(out) == [None, None, CE, None, None, None]

    def test_single_signal_firing_alone_never_confirms(self) -> None:
        day = self._day(3)
        votes = {"a": pd.Series([CE, None, None]), "b": pd.Series([None, None, None])}
        out = confluence_vote(votes, day=day, min_agree=2, window_bars=2)
        assert list(out) == [None, None, None]

    def test_ambiguous_bar_where_both_directions_clear_threshold_abstains(self) -> None:
        day = self._day(2)
        votes = {
            "a": pd.Series([CE, None]),
            "b": pd.Series([None, CE]),
            "c": pd.Series([None, PE]),
            "d": pd.Series([PE, None]),
        }
        out = confluence_vote(votes, day=day, min_agree=2, window_bars=2)
        assert list(out) == [None, None]

    def test_min_agree_3_requires_three_distinct_signals(self) -> None:
        day = self._day(3)
        votes = {
            "a": pd.Series([CE, None, None]),
            "b": pd.Series([None, CE, None]),
            "c": pd.Series([None, None, CE]),
        }
        out2 = confluence_vote(votes, day=day, min_agree=2, window_bars=2)
        out3 = confluence_vote(votes, day=day, min_agree=3, window_bars=2)
        assert list(out2) == [None, CE, CE]
        assert list(out3) == [None, None, CE]

    def test_day_boundary_prevents_agreement_across_days(self) -> None:
        day = pd.Series(["d1", "d1", "d2", "d2"])
        votes = {
            "a": pd.Series([None, CE, None, None]),
            "b": pd.Series([None, None, CE, None]),
        }
        out = confluence_vote(votes, day=day, min_agree=2, window_bars=2)
        # bar 2 is the first bar of d2; "a"'s fire at bar 1 belongs to d1 and
        # must not count toward d2's window.
        assert out.iloc[2] is None

    def test_min_agree_below_2_raises(self) -> None:
        day = self._day(2)
        votes = {"a": pd.Series([CE, None])}
        with pytest.raises(ValueError):
            confluence_vote(votes, day=day, min_agree=1)


# ── "If time permits" family: ADX +DI/-DI, Bollinger breakout, Supertrend ──

class TestComputeDiAndAdx:
    def test_matches_feature_engine_adx_directly_on_a_single_day(self) -> None:
        from snapshot_app.core.feature_engine import _adx as fe_adx

        rng = np.random.default_rng(11)
        n = 40
        close = 100 + np.cumsum(rng.normal(0, 1, n))
        high = close + rng.uniform(0.1, 1.0, n)
        low = close - rng.uniform(0.1, 1.0, n)
        df = pd.DataFrame({
            "trade_date": ["2026-01-05"] * n, "bar_index": range(n),
            "fut_high": high, "fut_low": low, "fut_close": close,
        })
        out = compute_di_and_adx(df)
        expected_adx = fe_adx(pd.Series(high), pd.Series(low), pd.Series(close), 14)
        np.testing.assert_allclose(out["adx_14_fresh"].to_numpy(), expected_adx.to_numpy(), equal_nan=True)

    def test_pure_uptrend_has_zero_minus_di_and_positive_plus_di(self) -> None:
        n = 20
        high = np.arange(100, 100 + n, dtype=float) + 1.0
        low = np.arange(100, 100 + n, dtype=float)
        close = np.arange(100, 100 + n, dtype=float) + 0.5
        df = pd.DataFrame({
            "trade_date": ["2026-01-05"] * n, "bar_index": range(n),
            "fut_high": high, "fut_low": low, "fut_close": close,
        })
        out = compute_di_and_adx(df)
        assert (out["minus_di"].iloc[1:] == 0.0).all()
        assert (out["plus_di"].iloc[1:] > 0.0).all()

    def test_pure_downtrend_has_zero_plus_di_and_positive_minus_di(self) -> None:
        n = 20
        high = np.arange(100, 100 - n, -1, dtype=float)
        low = high - 1.0
        close = high - 0.5
        df = pd.DataFrame({
            "trade_date": ["2026-01-05"] * n, "bar_index": range(n),
            "fut_high": high, "fut_low": low, "fut_close": close,
        })
        out = compute_di_and_adx(df)
        assert (out["plus_di"].iloc[1:] == 0.0).all()
        assert (out["minus_di"].iloc[1:] > 0.0).all()

    def test_di_cross_fires_ce_after_a_downtrend_reverses_to_an_uptrend(self) -> None:
        n_down, n_up = 20, 20
        down_high = np.arange(120, 120 - n_down, -1, dtype=float)
        down_low = down_high - 1.0
        down_close = down_high - 0.5
        up_start = down_high[-1]
        up_high = np.arange(up_start, up_start + n_up, dtype=float) + 1.0
        up_low = np.arange(up_start, up_start + n_up, dtype=float)
        up_close = np.arange(up_start, up_start + n_up, dtype=float) + 0.5
        high = np.concatenate([down_high, up_high])
        low = np.concatenate([down_low, up_low])
        close = np.concatenate([down_close, up_close])
        n = len(close)
        df = pd.DataFrame({
            "trade_date": ["2026-01-05"] * n, "bar_index": range(n),
            "fut_high": high, "fut_low": low, "fut_close": close,
        })
        di = compute_di_and_adx(df)
        df = pd.concat([df, di], axis=1)
        votes = adx_di_cross_vote(df)
        fires = [(i, v) for i, v in enumerate(votes) if v is not None]
        assert any(v == CE for _, v in fires), f"expected at least one CE fire after the reversal, got {fires}"
        # no PE fire should occur once the sustained uptrend is well underway
        late_fires = [v for i, v in fires if i >= n_down + 5]
        assert PE not in late_fires

    def test_min_adx_gate_suppresses_weak_trend_crosses(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1"] * 3,
            "plus_di": [10.0, 30.0, 30.0],
            "minus_di": [20.0, 20.0, 20.0],
            "adx_14_fresh": [5.0, 5.0, 5.0],
        })
        votes_ungated = adx_di_cross_vote(df)
        votes_gated = adx_di_cross_vote(df, min_adx=15.0)
        assert list(votes_ungated) == [None, CE, None]
        assert list(votes_gated) == [None, None, None]


class TestComputeBollingerBands:
    def test_identical_days_produce_identical_bands_no_cross_day_leak(self) -> None:
        rng = np.random.default_rng(3)
        prices = list(100 + np.cumsum(rng.normal(0, 1, 30)))
        df = _two_day_frame(prices, prices)
        out = compute_bollinger_bands(df)
        for col in ("bb_mid", "bb_upper", "bb_lower"):
            day1 = out[col].iloc[:30].to_numpy()
            day2 = out[col].iloc[30:].to_numpy()
            np.testing.assert_allclose(day1, day2, equal_nan=True)

    def test_flat_series_has_zero_width_bands_equal_to_price(self) -> None:
        prices = [100.0] * 10
        df = pd.DataFrame({"trade_date": ["d1"] * 10, "bar_index": range(10), "fut_close": prices})
        out = compute_bollinger_bands(df)
        valid = out.dropna()
        assert (valid["bb_upper"] == valid["bb_mid"]).all()
        assert (valid["bb_lower"] == valid["bb_mid"]).all()


class TestBollingerBreakoutVote:
    def test_fires_ce_on_upper_breakout_and_pe_on_lower_breakout_at_exact_bar(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1"] * 4,
            "fut_close": [100.0, 105.0, 100.0, 94.0],
            "bb_upper": [102.0, 102.0, 102.0, 102.0],
            "bb_lower": [95.0, 95.0, 95.0, 95.0],
        })
        votes = bollinger_breakout_vote(df)
        assert list(votes) == [None, CE, None, PE]

    def test_day_boundary_prevents_bleed(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1", "d1", "d2", "d2"],
            "fut_close": [100.0, 110.0, 100.0, 100.0],  # d1 closes deep above its own upper band
            "bb_upper": [102.0, 102.0, 102.0, 102.0],
            "bb_lower": [95.0, 95.0, 95.0, 95.0],
        })
        votes = bollinger_breakout_vote(df)
        assert votes.iloc[2] is None  # first bar of d2 must not fire off d1's breakout state


class TestPctbBreakoutVote:
    def test_fires_ce_above_1_and_pe_below_0_at_the_exact_bar(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1"] * 4,
            "pctb": [0.5, 1.2, 0.5, -0.3],
        })
        votes = pctb_breakout_vote(df, pctb_col="pctb")
        assert list(votes) == [None, CE, None, PE]

    def test_day_boundary_prevents_bleed(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1", "d1", "d2", "d2"],
            "pctb": [0.5, 1.5, 0.5, 0.5],  # d1 ends well above upper band
        })
        votes = pctb_breakout_vote(df, pctb_col="pctb")
        assert votes.iloc[2] is None

    def test_custom_levels(self) -> None:
        df = pd.DataFrame({"trade_date": ["d1"] * 3, "pctb": [0.2, 0.05, 0.2]})
        votes = pctb_breakout_vote(df, pctb_col="pctb", lower_level=0.1, upper_level=0.9)
        assert list(votes) == [None, PE, None]


class TestComputeSupertrend:
    def test_identical_days_produce_identical_trend_no_cross_day_leak(self) -> None:
        high = [10, 10, 8, 6, 12, 11, 13]
        low = [8, 9, 6, 4, 10, 9, 11]
        close = [9, 9.5, 6.5, 4.5, 11.5, 10, 12]
        df = pd.DataFrame({
            "trade_date": ["d1"] * 7 + ["d2"] * 7,
            "bar_index": list(range(7)) * 2,
            "fut_high": high * 2, "fut_low": low * 2, "fut_close": close * 2,
        })
        out = compute_supertrend(df, period=1, multiplier=1.0)
        day1 = out["supertrend_trend"].iloc[:7].to_numpy()
        day2 = out["supertrend_trend"].iloc[7:].to_numpy()
        np.testing.assert_array_equal(day1, day2)

    def test_hand_verified_flip_sequence_with_period_1_multiplier_1(self) -> None:
        # Hand-derived (see crossover_direction module review notes / PR
        # description): with period=1, ATR reduces to the bar's own true
        # range (ewm alpha=1 tracks the latest observation exactly), making
        # the recursive band-ratchet arithmetic tractable to verify by hand.
        high = [10, 10, 8, 6, 12]
        low = [8, 9, 6, 4, 10]
        close = [9, 9.5, 6.5, 4.5, 11.5]
        df = pd.DataFrame({
            "trade_date": ["d1"] * 5, "bar_index": range(5),
            "fut_high": high, "fut_low": low, "fut_close": close,
        })
        out = compute_supertrend(df, period=1, multiplier=1.0)
        np.testing.assert_array_equal(out["supertrend_trend"].to_numpy(), [1, 1, -1, -1, 1])
        np.testing.assert_allclose(
            out["supertrend_line"].to_numpy(), [7.0, 8.5, 10.5, 7.5, 3.5],
        )
        df["supertrend_trend"] = out["supertrend_trend"]
        votes = supertrend_flip_vote(df)
        assert list(votes) == [None, None, PE, None, CE]

    def test_day_boundary_first_bar_never_flips(self) -> None:
        df = pd.DataFrame({
            "trade_date": ["d1", "d2"],
            "supertrend_trend": [-1, 1],
        })
        votes = supertrend_flip_vote(df)
        assert votes.iloc[1] is None
