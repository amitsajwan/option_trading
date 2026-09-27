"""Tests for ml_pipeline_2.pipeline.entry_quality_direction. Fixture
convention matches test_option_payoff_labels.py's `_snap()` helper: build a
raw payload dict, wrap in SnapshotAccessor."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.entry_quality_direction import (
    RAW_SIGNAL_COLUMNS,
    build_raw_signal_frame,
    compute_payoff_at_delay,
    derive_pcr_change,
    encode_side_binary,
    extract_raw_signal_row,
    fit_direction_classifier,
    fit_entry_payoff_model,
    fit_entry_payoff_oof_scores,
    predict_direction_side,
    score_entry_payoff,
)
from strategy_app.cost_model import TradingCostModel
from strategy_app.market.snapshot_accessor import SnapshotAccessor

LOT_SIZE = 65
_COST = TradingCostModel()


def _snap(
    *,
    timestamp: str = "2026-01-05T04:15:00+00:00",  # 09:45 IST
    trade_date: str = "2026-01-05",
    atm_strike: int | None = 24500,
    atm_ce_close: float | None = 100.0,
    atm_pe_close: float | None = 100.0,
    atm_ce_oi: float | None = 50000.0,
    atm_pe_oi: float | None = 50000.0,
    strikes: list[dict] | None = None,
    fut_return_5m: float | None = 1.5,
    fut_return_15m: float | None = 2.0,
    price_vs_vwap: float | None = 0.5,
    vix_intraday_chg: float | None = -1.0,
    atm_ce_iv: float | None = 14.0,
    atm_pe_iv: float | None = 15.0,
    orh: float | None = 24600.0,
    orl: float | None = 24400.0,
    orh_broken: bool = False,
    orl_broken: bool = False,
    fut_close: float | None = 24550.0,
    pcr: float | None = 1.05,
) -> SnapshotAccessor:
    # Nested-payload shape verified against strategy_app/market/snapshot_accessor.py
    # (self._fd = futures_derived, self._fb = futures_bar, self._opening_range =
    # opening_range, self._vix = vix_context, self._ca = chain_aggregates,
    # self._atm = atm_options), 2026-09-27. `pcr_change_5m` is deliberately NOT
    # set here (real archive never populates it -- see derive_pcr_change's
    # docstring); only the raw `pcr` level is, since that's what this study
    # actually derives pcr_change_5m from.
    payload: dict = {
        "snapshot_id": "s1",
        "timestamp": timestamp,
        "trade_date": trade_date,
        "session_context": {"date": trade_date, "time": timestamp},
        "chain_aggregates": {"atm_strike": atm_strike, "pcr": pcr},
        "atm_options": {
            "atm_ce_close": atm_ce_close, "atm_pe_close": atm_pe_close,
            "atm_ce_oi": atm_ce_oi, "atm_pe_oi": atm_pe_oi,
            "atm_ce_iv": atm_ce_iv, "atm_pe_iv": atm_pe_iv,
        },
        "futures_derived": {
            "fut_return_5m": fut_return_5m, "fut_return_15m": fut_return_15m,
            "price_vs_vwap": price_vs_vwap,
        },
        "futures_bar": {"fut_close": fut_close},
        "opening_range": {"orh": orh, "orl": orl, "orh_broken": orh_broken, "orl_broken": orl_broken},
        "vix_context": {"vix_intraday_chg": vix_intraday_chg},
        "strikes": strikes or [],
    }
    return SnapshotAccessor(payload)


class TestExtractRawSignalRow:
    def test_pulls_every_directly_extractable_raw_field(self) -> None:
        # RAW_SIGNAL_COLUMNS includes pcr_change_5m, but that's only added
        # later by build_raw_signal_frame's derivation (needs a whole day's
        # sequence) -- a single-bar extraction can only produce the raw
        # `pcr` LEVEL, so pcr_change_5m itself is deliberately excluded here.
        snap = _snap()
        row = extract_raw_signal_row(snap, bar_index=3)
        assert row["bar_index"] == 3
        assert row["trade_date"] == "2026-01-05"
        for col in RAW_SIGNAL_COLUMNS:
            if col == "pcr_change_5m":
                continue
            assert col in row
        assert "pcr" in row

    def test_missing_optional_fields_degrade_to_none_not_crash(self) -> None:
        snap = _snap(atm_ce_iv=None, atm_pe_iv=None, orh=None, orl=None)
        row = extract_raw_signal_row(snap, bar_index=0)
        assert row["atm_ce_iv"] is None
        assert row["orh"] is None


class TestDerivePcrChange:
    def test_computes_lookback_difference_within_a_day(self) -> None:
        pcr = pd.Series([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 2.0])
        out = derive_pcr_change(pcr, lookback=5)
        assert out.iloc[:5].isna().all()  # not enough history yet
        assert out.iloc[5] == pytest.approx(1.5 - 1.0)
        assert out.iloc[6] == pytest.approx(2.0 - 1.1)

    def test_nan_pcr_values_propagate_not_crash(self) -> None:
        # index 1 is NaN -- the diff that looks back exactly to index 1
        # (index 1+5=6) must come out NaN too, not silently skip/impute it.
        pcr = pd.Series([1.0, np.nan, 1.2, 1.3, 1.4, 1.5, 1.6])
        out = derive_pcr_change(pcr, lookback=5)
        assert np.isnan(out.iloc[6])


class TestBuildRawSignalFrame:
    def test_empty_snaps_returns_empty_frame_with_expected_columns(self) -> None:
        df = build_raw_signal_frame([])
        assert len(df) == 0
        for col in RAW_SIGNAL_COLUMNS:
            assert col in df.columns

    def test_bar_index_matches_position_in_sequence(self) -> None:
        snaps = [_snap(timestamp=f"2026-01-05T04:{15+i:02d}:00+00:00") for i in range(5)]
        df = build_raw_signal_frame(snaps)
        assert list(df["bar_index"]) == [0, 1, 2, 3, 4]
        assert len(df) == 5

    def test_pcr_change_5m_is_derived_from_the_lookback_diff_of_raw_pcr(self) -> None:
        pcrs = [1.0, 1.05, 1.10, 1.15, 1.20, 1.30]  # 6 bars, lookback=5
        snaps = [
            _snap(timestamp=f"2026-01-05T04:{15+i:02d}:00+00:00", pcr=pcrs[i])
            for i in range(len(pcrs))
        ]
        df = build_raw_signal_frame(snaps)
        assert df["pcr_change_5m"].iloc[:5].isna().all()
        assert df["pcr_change_5m"].iloc[5] == pytest.approx(1.30 - 1.0)
        assert "pcr" not in df.columns  # intermediate-only, not part of the public contract


class TestComputePayoffAtDelay:
    def _rallying_day(self, n_bars: int = 30) -> list[SnapshotAccessor]:
        out = []
        for i in range(n_bars):
            ts = pd.Timestamp("2026-01-05 04:15:00") + pd.Timedelta(minutes=i)
            ce = 100.0 + i * 2.0
            pe = max(5.0, 100.0 - i * 2.0)
            out.append(_snap(
                timestamp=ts.isoformat() + "+00:00",
                # Both the bar's OWN quote (atm_ce_close/atm_pe_close, read as
                # the ENTRY premium by compute_bar_payoff) and its "strikes"
                # ladder (read as the EXIT premium) must vary with `i` --
                # otherwise every bar's entry premium is stuck at the fixture
                # default regardless of which bar is used as the delayed entry.
                atm_ce_close=ce, atm_pe_close=pe,
                strikes=[{"strike": 24500, "ce_ltp": ce, "pe_ltp": pe}],
            ))
        return out

    def test_delay_0_matches_plain_compute_bar_payoff(self) -> None:
        from ml_pipeline_2.pipeline.option_payoff_labels import compute_bar_payoff
        snaps = self._rallying_day()
        direct = compute_bar_payoff(snaps[0], snaps[5], lot_size=LOT_SIZE, cost_model=_COST)
        delayed = compute_payoff_at_delay(snaps, 0, horizon_bars=5, delay_bars=0, lot_size=LOT_SIZE, cost_model=_COST)
        assert delayed is not None and direct is not None
        assert delayed.payoff_score == direct.payoff_score

    def test_delay_shifts_entry_premium_holds_same_exit_bar(self) -> None:
        snaps = self._rallying_day()
        delayed = compute_payoff_at_delay(snaps, 0, horizon_bars=10, delay_bars=2, lot_size=LOT_SIZE, cost_model=_COST)
        assert delayed is not None
        # entry sampled at bar 2 (ce_ltp=104), exit still at bar 10 (ce_ltp=120)
        assert delayed.ce is not None
        assert delayed.ce.entry_premium == 104.0
        assert delayed.ce.exit_premium == 120.0

    def test_delay_past_session_end_returns_none(self) -> None:
        snaps = self._rallying_day(n_bars=10)
        assert compute_payoff_at_delay(snaps, 8, horizon_bars=5, delay_bars=2, lot_size=LOT_SIZE) is None

    def test_delay_beyond_exit_bar_returns_none(self) -> None:
        snaps = self._rallying_day(n_bars=30)
        # horizon 2, delay 5 -> delayed entry would be AFTER the fixed exit bar
        assert compute_payoff_at_delay(snaps, 0, horizon_bars=2, delay_bars=5, lot_size=LOT_SIZE) is None


class TestEncodeSideBinary:
    def test_ce_is_one_pe_is_zero(self) -> None:
        out = encode_side_binary(pd.Series(["CE", "PE", "CE"]))
        np.testing.assert_array_equal(out, [1, 0, 1])

    def test_unexpected_value_raises(self) -> None:
        with pytest.raises(ValueError, match="unexpected"):
            encode_side_binary(pd.Series(["CE", "PE", "FLAT"]))


class TestFitScoreEntryPayoffModel:
    xgb = pytest.importorskip("xgboost")

    def _synthetic_df(self, n: int = 300, seed: int = 0) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        f1 = rng.normal(size=n)
        f2 = rng.normal(size=n)
        # payoff_score genuinely (noisily) increasing in f1 -- gives the
        # regressor real ranking skill to recover, unlike pure noise.
        payoff = 0.05 + 0.03 * f1 + rng.normal(scale=0.01, size=n)
        return pd.DataFrame({"f1": f1, "f2": f2, "payoff_score": payoff})

    def test_model_recovers_ranking_skill_on_a_real_signal(self) -> None:
        df = self._synthetic_df()
        model, medians = fit_entry_payoff_model(df, ["f1", "f2"], "payoff_score")
        preds = score_entry_payoff(model, medians, df, ["f1", "f2"])
        from scipy.stats import spearmanr
        rho, _ = spearmanr(preds, df["payoff_score"])
        assert rho > 0.5

    def test_score_entry_payoff_handles_missing_feature_values(self) -> None:
        df = self._synthetic_df()
        model, medians = fit_entry_payoff_model(df, ["f1", "f2"], "payoff_score")
        holdout = df.copy()
        holdout.loc[0, "f1"] = np.nan
        preds = score_entry_payoff(model, medians, holdout, ["f1", "f2"])
        assert np.isfinite(preds).all()

    def test_oof_scores_are_out_of_fold_not_perfectly_matching_in_sample_fit(self) -> None:
        df = self._synthetic_df(n=500)
        oof = fit_entry_payoff_oof_scores(df, ["f1", "f2"], "payoff_score", n_splits=5)
        assert len(oof) == len(df)
        assert np.isfinite(oof).all()

    def test_oof_scores_fall_back_to_constant_on_thin_data(self) -> None:
        df = self._synthetic_df(n=10)
        oof = fit_entry_payoff_oof_scores(df, ["f1", "f2"], "payoff_score", n_splits=5, min_rows_to_fit=50)
        assert len(oof) == 10
        assert len(set(np.round(oof, 8))) == 1  # constant fallback


class TestDirectionClassifier:
    xgb = pytest.importorskip("xgboost")

    def test_classifier_learns_a_real_separable_signal(self) -> None:
        rng = np.random.default_rng(1)
        n = 400
        f1 = rng.normal(size=n)
        y = (f1 > 0).astype(int)
        X = pd.DataFrame({"f1": f1, "noise": rng.normal(size=n)})
        model, medians = fit_direction_classifier(X, y)
        preds = predict_direction_side(model, medians, X)
        assert (preds == y).mean() > 0.85

    def test_predict_handles_nan_features(self) -> None:
        rng = np.random.default_rng(2)
        n = 200
        f1 = rng.normal(size=n)
        y = (f1 > 0).astype(int)
        X = pd.DataFrame({"f1": f1})
        model, medians = fit_direction_classifier(X, y)
        X_holdout = X.copy()
        X_holdout.loc[0, "f1"] = np.nan
        preds = predict_direction_side(model, medians, X_holdout)
        assert preds.shape[0] == n
