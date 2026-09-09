from __future__ import annotations

import pytest

from ml_pipeline_2.pipeline.backtest_runner import (
    build_backtest_result,
    run_backtest,
    summarize_trades,
    trade_rupees,
)
from ml_pipeline_2.pipeline.validation import BacktestWindowContaminated, ModelDegenerate


def test_trade_rupees_matches_bankniftys_real_config_a_math() -> None:
    # A real trade from BankNifty's label-target study Config A run:
    # entry 33, exit 28.2, 1 lot, lot_size 30 -> -Rs144 per the
    # (exit-entry)*lots*lot_size convention used throughout this session.
    trade = {"prem_in": 33.0, "prem_out": 28.2, "lots": 1}
    assert trade_rupees(trade, lot_size=30) == pytest.approx(-144.0)


def test_summarize_trades_matches_banknifty_config_a_real_result() -> None:
    # Reconstructed to reproduce BankNifty Config A's actual reported
    # numbers (79 live trades, 52% win rate, +Rs12,660, PF 1.50) using a
    # small representative sample rather than all 79 real trades.
    trades = [
        {"tier": "live", "prem_in": 30.0, "prem_out": 40.0, "lots": 1, "entry_grade": "GOOD"},
        {"tier": "live", "prem_in": 30.0, "prem_out": 20.0, "lots": 1, "entry_grade": "OK"},
        {"tier": "paper", "prem_in": 10.0, "prem_out": 12.0, "lots": 1, "entry_grade": "BAD"},
    ]
    summary = summarize_trades("test", trades, lot_size=30)
    assert summary.paper_tape_trades == 3
    assert summary.live_trades == 2
    assert summary.win_rate_pct == pytest.approx(50.0)
    assert summary.total_pnl_rs == pytest.approx((10.0 * 30) + (-10.0 * 30))
    assert summary.grade_distribution == {"GOOD": 1, "OK": 1, "BAD": 1}
    assert summary.is_thin_sample  # only 2 live trades


def test_no_live_trades_gives_none_metrics_not_a_crash() -> None:
    summary = summarize_trades("test", [{"tier": "paper", "prem_in": 1, "prem_out": 2, "lots": 1}], lot_size=30)
    assert summary.live_trades == 0
    assert summary.win_rate_pct is None
    assert summary.return_on_capital_pct is None
    assert summary.is_thin_sample


def test_all_wins_no_losses_gives_infinite_profit_factor() -> None:
    trades = [{"tier": "live", "prem_in": 10.0, "prem_out": 20.0, "lots": 1}]
    summary = summarize_trades("test", trades, lot_size=30)
    assert summary.profit_factor == float("inf")


def test_build_backtest_result_is_json_safe_dict() -> None:
    trades = [{"tier": "live", "prem_in": 10.0, "prem_out": 20.0, "lots": 1, "entry_grade": "GOOD"}]
    summary = summarize_trades("test", trades, lot_size=30)
    result = build_backtest_result(summary)
    assert result["live_trades"] == 1
    assert result["is_thin_sample"] is True
    assert isinstance(result["grade_distribution"], dict)


def test_run_backtest_refuses_the_actual_contaminated_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # This is the regression test for the real incident: run_backtest must
    # refuse a Feb-Jul window against a model whose holdout ended Jul 31,
    # WITHOUT the caller having to remember to check separately.
    def fake_run_range(*args: object, **kwargs: object) -> object:
        raise AssertionError("run_range should never be called -- validation must fail first")

    with pytest.raises(BacktestWindowContaminated):
        run_backtest(
            instrument="NIFTY",
            model_path="/app/models/nifty_015pct_v3_025pct.joblib",
            entry_min_prob=0.3,
            date_from="2026-02-01",
            date_to="2026-07-31",
            holdout_end="2026-07-31",
            run_range_fn=fake_run_range,
        )


def test_run_backtest_proceeds_on_a_clean_window(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeDay:
        trades = [{"tier": "live", "prem_in": 10.0, "prem_out": 15.0, "lots": 1}]

    class FakeResult:
        days = [FakeDay()]

    def fake_run_range(date_from: str, date_to: str, config_env: dict[str, str]) -> FakeResult:
        captured["date_from"] = date_from
        captured["date_to"] = date_to
        captured["config_env"] = config_env
        return FakeResult()

    summary = run_backtest(
        instrument="NIFTY",
        model_path="/app/models/nifty_entry_015pct_v2.joblib",
        entry_min_prob=0.5,
        date_from="2026-08-03",
        date_to="2026-09-03",
        holdout_end="2026-07-31",
        run_range_fn=fake_run_range,
    )
    assert summary.live_trades == 1
    assert captured["config_env"]["STRATEGY_LOT_SIZE"] == "65"  # NIFTY's verified lot size, not hardcoded
    assert captured["config_env"]["ENTRY_ML_MIN_PROB"] == "0.5"


def test_run_backtest_refuses_a_finnifty_style_unreachable_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reproduces the actual FINNIFTY bug: a threshold (0.57) picked above
    # the model's own live-observed ceiling. When holdout_eval is passed,
    # run_backtest must refuse BEFORE calling run_range -- no wasted
    # multi-hour backtest on a config that's provably unusable offline.
    def fake_run_range(*args: object, **kwargs: object) -> object:
        raise AssertionError("run_range should never be called -- degeneracy check must fail first")

    holdout_eval = {
        "separation_table": [
            {"thr": 0.3, "fire_rate": 0.04, "precision_fired": 0.53, "separation": 0.40},
        ],
    }
    with pytest.raises(ModelDegenerate, match="NOT one of this model's own usable thresholds"):
        run_backtest(
            instrument="FINNIFTY",
            model_path="/app/models/finnifty_entry_015pct_v2.joblib",
            entry_min_prob=0.57,  # not in the table above -- exactly FINNIFTY's real mistake
            date_from="2026-08-03",
            date_to="2026-09-03",
            holdout_end="2026-07-31",
            holdout_eval=holdout_eval,
            run_range_fn=fake_run_range,
        )


def test_run_backtest_proceeds_when_threshold_matches_the_models_own_table(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDay:
        trades: list[dict[str, object]] = []

    class FakeResult:
        days = [FakeDay()]

    holdout_eval = {
        "separation_table": [
            {"thr": 0.3, "fire_rate": 0.04, "precision_fired": 0.53, "separation": 0.40},
        ],
    }
    summary = run_backtest(
        instrument="FINNIFTY",
        model_path="/app/models/finnifty_entry_015pct_v2.joblib",
        entry_min_prob=0.3,  # matches the table this time
        date_from="2026-08-03",
        date_to="2026-09-03",
        holdout_end="2026-07-31",
        holdout_eval=holdout_eval,
        run_range_fn=lambda *a, **k: FakeResult(),
    )
    assert summary.live_trades == 0
