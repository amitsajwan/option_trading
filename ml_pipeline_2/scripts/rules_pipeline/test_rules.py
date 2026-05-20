from __future__ import annotations

import pandas as pd
import pytest

from ml_pipeline_2.scripts.rules_pipeline.condition_evaluator import (
    evaluate_all_and,
    evaluate_any_or,
    evaluate_condition,
)
from ml_pipeline_2.scripts.rules_pipeline.rule_schema import Condition, ExitConfig, Rule
from ml_pipeline_2.scripts.rules_pipeline.signal_generator import generate_signals


class TestCondition:
    def test_literal_comparison(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4, 5]})
        cond = Condition("a", ">", 3)
        result = evaluate_condition(df, cond)
        assert result.tolist() == [False, False, False, True, True]

    def test_cross_column_comparison(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [0, 2, 5]})
        cond = Condition("a", ">", "b")
        result = evaluate_condition(df, cond)
        assert result.tolist() == [True, False, False]

    def test_equality(self):
        df = pd.DataFrame({"a": [1, 0, 1, 0]})
        cond = Condition("a", "==", 1)
        result = evaluate_condition(df, cond)
        assert result.tolist() == [True, False, True, False]

    def test_nan_handling(self):
        df = pd.DataFrame({"a": [1.0, None, 3.0]})
        cond = Condition("a", ">", 2)
        result = evaluate_condition(df, cond)
        assert result.tolist() == [False, False, True]

    def test_unknown_operator(self):
        df = pd.DataFrame({"a": [1]})
        cond = Condition("a", "??", 1)
        with pytest.raises(ValueError, match="Unknown operator"):
            evaluate_condition(df, cond)


class TestAllAnd:
    def test_empty_conditions(self):
        df = pd.DataFrame({"a": [1, 2]})
        result = evaluate_all_and(df, [])
        assert result.tolist() == [True, True]

    def test_single_condition(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        result = evaluate_all_and(df, [Condition("a", ">", 1)])
        assert result.tolist() == [False, True, True]

    def test_multiple_conditions(self):
        df = pd.DataFrame({"a": [1, 3, 5], "b": [0, 4, 6]})
        conds = [Condition("a", ">", 2), Condition("b", ">", 3)]
        result = evaluate_all_and(df, conds)
        assert result.tolist() == [False, True, True]


class TestAnyOr:
    def test_empty_conditions(self):
        df = pd.DataFrame({"a": [1, 2]})
        result = evaluate_any_or(df, [])
        assert result.tolist() == [False, False]

    def test_single_condition(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        result = evaluate_any_or(df, [Condition("a", ">", 1)])
        assert result.tolist() == [False, True, True]

    def test_multiple_conditions(self):
        df = pd.DataFrame({"a": [0, 0, 5], "b": [0, 4, 0]})
        conds = [Condition("a", ">", 2), Condition("b", ">", 2)]
        result = evaluate_any_or(df, conds)
        assert result.tolist() == [False, True, True]


class TestRuleFromDict:
    def test_minimal_rule(self):
        d = {
            "rule_id": "R1",
            "direction": "BUY_ATM_PE",
            "entry_conditions": [{"column": "a", "operator": ">", "value": 0}],
            "disqualifiers": [],
            "exit_mechanical": {
                "stop_pct": 20,
                "target_pct": 35,
                "time_stop_minutes": 15,
                "eod_force_close_minute": 370,
            },
        }
        rule = Rule.from_dict(d)
        assert rule.rule_id == "R1"
        assert rule.direction == "BUY_ATM_PE"
        assert len(rule.entry_conditions) == 1
        assert len(rule.disqualifiers) == 0
        assert rule.exit_mechanical.stop_pct == 20
        assert rule.exit_signal is None

    def test_with_signal_exits(self):
        d = {
            "rule_id": "R1",
            "direction": "BUY_ATM_PE",
            "entry_conditions": [{"column": "a", "operator": ">", "value": 0}],
            "disqualifiers": [{"column": "b", "operator": "==", "value": 1}],
            "exit_mechanical": {
                "stop_pct": 20,
                "target_pct": 35,
                "time_stop_minutes": 15,
                "eod_force_close_minute": 370,
            },
            "exit_signal": {
                "stop_pct": 20,
                "target_pct": 35,
                "time_stop_minutes": 15,
                "eod_force_close_minute": 370,
                "signal_exits": [
                    {"column": "c", "operator": ">", "value": "d"},
                ],
            },
        }
        rule = Rule.from_dict(d)
        assert rule.exit_signal is not None
        assert len(rule.exit_signal.signal_exits) == 1


class TestGenerateSignals:
    def test_no_disqualifiers(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4]})
        rule = Rule(
            rule_id="T1",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("a", ">", 2),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(20, 35, 15, 370),
        )
        result = generate_signals(df, rule)
        assert result.tolist() == [False, False, True, True]

    def test_with_disqualifier(self):
        df = pd.DataFrame({"a": [1, 2, 3, 4], "b": [0, 1, 0, 1]})
        rule = Rule(
            rule_id="T1",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("a", ">", 1),),
            disqualifiers=(Condition("b", "==", 1),),
            exit_mechanical=ExitConfig(20, 35, 15, 370),
        )
        result = generate_signals(df, rule)
        assert result.tolist() == [False, False, True, False]

    def test_all_disqualified(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [1, 1, 1]})
        rule = Rule(
            rule_id="T1",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("a", ">", 0),),
            disqualifiers=(Condition("b", "==", 1),),
            exit_mechanical=ExitConfig(20, 35, 15, 370),
        )
        result = generate_signals(df, rule)
        assert result.tolist() == [False, False, False]


def _build_one_day_df(minutes, ce_closes, *, extra_cols=None) -> pd.DataFrame:
    """Helper: build a single-trade-date df with the columns simulate_trades needs."""
    n = len(minutes)
    base = {
        "trade_date": ["2024-08-15"] * n,
        "minute": minutes,
        "signal": [False] * n,
        "ce_close": ce_closes,
        "pe_close": [0.0] * n,
    }
    if extra_cols:
        base.update(extra_cols)
    return pd.DataFrame(base)


class TestExecutionSim:
    """Regression tests for execution_sim bugs fixed 2026-05-20."""

    def test_cross_column_signal_exit_fires(self):
        """Fix #1: signal-exit with cross-column RHS must resolve against the row's
        other columns, not crash silently."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        # 10 minutes, CE rises 100 -> 105 then vwap_distance flips negative at minute 5.
        df = _build_one_day_df(
            minutes=list(range(555, 565)),
            ce_closes=[100, 101, 102, 103, 104, 105, 105, 105, 105, 105],
            extra_cols={"vwap_distance": [1, 1, 1, 1, 1, -1, -1, -1, -1, -1]},
        )
        df.loc[0, "signal"] = True  # entry at minute 555

        rule = Rule(
            rule_id="X1",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=50, target_pct=50, time_stop_minutes=999,
                eod_force_close_minute=999,
                signal_exits=(Condition("vwap_distance", "<", 0),),
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical")
        assert len(trades) == 1
        assert trades.iloc[0]["exit_reason"] == "signal:vwap_distance"
        # exit at minute 560 (the first row where vwap_distance < 0)
        assert int(trades.iloc[0]["exit_minute"]) == 560

    def test_eod_fallback_returns_last_same_day_premium(self):
        """Fix #2: if no mechanical exit triggers and day ends, use the last
        same-day premium, not crash on an undefined loop variable."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        # Entry at minute 555 with very loose exits — nothing should trigger.
        # Day has 5 rows total; expect eod_force at the last one.
        df = _build_one_day_df(
            minutes=[555, 556, 557, 558, 559],
            ce_closes=[100, 101, 102, 103, 104],
        )
        df.loc[0, "signal"] = True

        rule = Rule(
            rule_id="X2",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=99, target_pct=99,
                time_stop_minutes=999, eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical")
        assert len(trades) == 1
        assert trades.iloc[0]["exit_reason"] == "eod_force"
        assert int(trades.iloc[0]["exit_minute"]) == 559
        assert float(trades.iloc[0]["exit_premium"]) == 104.0

    def test_real_data_smoke_one_day(self):
        """Fix #4: integration smoke test — only runs on the ML VM where the
        parquet data lives. Asserts the merge actually returns non-null
        ce_close/pe_close on a real trading day (no silent zero-row joins)."""
        from ml_pipeline_2.scripts.rules_pipeline.data_loader import (
            DEFAULT_FLAT_ROOT,
            DEFAULT_OPTIONS_ROOT,
            load_merged_data_both,
        )
        if not DEFAULT_FLAT_ROOT.exists() or not DEFAULT_OPTIONS_ROOT.exists():
            pytest.skip("ML VM data not present locally")

        df = load_merged_data_both(
            DEFAULT_FLAT_ROOT, DEFAULT_OPTIONS_ROOT,
            start_date="2024-08-15", end_date="2024-08-15",
        )
        assert len(df) > 300, f"expected ~375 minute bars, got {len(df)}"
        # Uniqueness on (trade_date, minute) — would expose merge inflation
        assert not df.duplicated(subset=["trade_date", "minute"]).any()
        # At least 80% of rows should have both CE and PE premiums populated
        ce_pop = df["ce_close"].notna().mean()
        pe_pop = df["pe_close"].notna().mean()
        assert ce_pop >= 0.8, f"ce_close populated only {ce_pop:.1%}"
        assert pe_pop >= 0.8, f"pe_close populated only {pe_pop:.1%}"
        # Premiums should be plausible BankNifty ATM weekly numbers
        assert df["ce_close"].dropna().between(1, 5000).all()
        assert df["pe_close"].dropna().between(1, 5000).all()

    def test_pipeline_enumerate_cells_cross_product(self):
        """Orchestrator: 2 rules × 2 windows × 1 exit_mode = 4 cells, with
        deterministic cell_ids derived from rule+window+exit_mode."""
        from ml_pipeline_2.scripts.rules_pipeline.pipeline import enumerate_cells

        cfg = {
            "rules": [
                {"rule_id": "R1", "path": "rules/r1.json"},
                {"rule_id": "R2", "path": "rules/r2.json"},
            ],
            "windows": [
                {"name": "may_jul_2024", "start": "2024-05-01", "end": "2024-07-31"},
                {"name": "aug_oct_2024", "start": "2024-08-01", "end": "2024-10-31"},
            ],
            "exit_modes": ["mechanical"],
        }
        cells = enumerate_cells(cfg)
        assert len(cells) == 4
        ids = sorted(c.cell_id for c in cells)
        assert ids == [
            "R1_aug_oct_2024_mechanical",
            "R1_may_jul_2024_mechanical",
            "R2_aug_oct_2024_mechanical",
            "R2_may_jul_2024_mechanical",
        ]

    def test_short_pnl_sign_inverts(self):
        """Short option: profit when premium drops. 100→90 should yield
        +0.10 pnl (less the 2bp cost), exit_reason 'target' if target hits."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        df = _build_one_day_df(minutes=[555, 556], ce_closes=[100, 90])
        df.loc[0, "signal"] = True
        rule = Rule(
            rule_id="S1",
            direction="SELL_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=100, target_pct=5, time_stop_minutes=999,
                eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical", cost_bps=2.0)
        assert len(trades) == 1
        # premium 100 → 90 means short profits +10% of credit; minus 2 bps
        assert abs(float(trades.iloc[0]["net_pnl_pct"]) - 0.0998) < 1e-6
        assert trades.iloc[0]["exit_reason"] == "target"

    def test_short_stop_on_premium_rise(self):
        """Short option: stop hits when premium rises. stop_pct=100 means
        premium has doubled (loss equals one credit)."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        df = _build_one_day_df(minutes=[555, 556, 557], ce_closes=[100, 150, 220])
        df.loc[0, "signal"] = True
        rule = Rule(
            rule_id="S2",
            direction="SELL_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=100, target_pct=50, time_stop_minutes=999,
                eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical", cost_bps=2.0)
        assert len(trades) == 1
        # premium 100 → 220 = +120% rise = -120% on the short. Stop at -100%.
        # Exit at minute 557 (the first row where pnl <= -1.00).
        assert trades.iloc[0]["exit_reason"] == "stop_loss"
        assert int(trades.iloc[0]["exit_minute"]) == 557

    def test_short_mfe_mae_directionality(self):
        """For a short, MFE is the largest favorable drop, MAE the worst
        adverse rise — both measured as fraction of credit."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        # premium walks: 100 → 105 (adverse +5%) → 95 (favorable +5%) → 110 (adverse +10%)
        # then EOD force-close at 110 (final adverse +10%).
        df = _build_one_day_df(minutes=[555, 556, 557, 558], ce_closes=[100, 105, 95, 110])
        df.loc[0, "signal"] = True
        rule = Rule(
            rule_id="S3",
            direction="SELL_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=99, target_pct=99,
                time_stop_minutes=999, eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical", cost_bps=0.0)
        assert len(trades) == 1
        assert abs(float(trades.iloc[0]["mfe_pct"]) - 0.05) < 1e-9   # best drop: 100→95
        assert abs(float(trades.iloc[0]["mae_pct"]) + 0.10) < 1e-9   # worst rise: 100→110
        # net is the EOD close at 110 → short is -10%
        assert abs(float(trades.iloc[0]["net_pnl_pct"]) + 0.10) < 1e-9

    def test_returns_in_decimal_units(self):
        """Fix #5: simulate_trades must emit returns as decimals (0.05 = +5%)
        to feed audit_run.audit directly with return_col='net_pnl_pct'.
        Cost of 2 bps must subtract exactly 0.0002 from gross."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        df = _build_one_day_df(minutes=[555, 556], ce_closes=[100, 110])
        df.loc[0, "signal"] = True
        rule = Rule(
            rule_id="X4",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=99, target_pct=5, time_stop_minutes=999,
                eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical", cost_bps=2.0)
        assert len(trades) == 1
        # +10% gross (100 → 110) - 2 bps cost = 0.1 - 0.0002 = 0.0998
        assert abs(float(trades.iloc[0]["net_pnl_pct"]) - 0.0998) < 1e-6, (
            f"expected ~0.0998 decimal, got {trades.iloc[0]['net_pnl_pct']}"
        )
        assert trades.iloc[0]["exit_reason"] == "target"

    def test_expiry_filter_keeps_nearest_forward_only(self):
        """Fix #3: options/ may contain multiple expiries per day. The filter
        must keep only the nearest forward expiry per trade_date so the merge
        on (trade_date, minute, strike) doesn't inflate."""
        from ml_pipeline_2.scripts.rules_pipeline.data_loader import _filter_to_chosen_expiry

        # Two trade_dates, each with two expiries present in the parquet.
        # 2024-08-15: weekly 22AUG24 (forward) + monthly 29AUG24 (forward) — keep 22AUG24.
        # 2024-08-20: 22AUG24 still forward, 29AUG24 also forward — keep 22AUG24.
        df = pd.DataFrame({
            "trade_date": pd.to_datetime([
                "2024-08-15", "2024-08-15", "2024-08-15", "2024-08-15",
                "2024-08-20", "2024-08-20", "2024-08-20", "2024-08-20",
            ]),
            "minute": [555, 555, 555, 555, 560, 560, 560, 560],
            "strike": [50000, 50000, 50000, 50000, 50100, 50100, 50100, 50100],
            "option_type": ["CE", "CE", "PE", "PE", "CE", "CE", "PE", "PE"],
            "close": [100, 200, 110, 210, 120, 220, 130, 230],
            "expiry_str": ["22AUG24", "29AUG24"] * 4,
        })
        filtered = _filter_to_chosen_expiry(df)
        # 4 rows survive — 2 (CE,PE) per trade_date, all on 22AUG24
        assert len(filtered) == 4
        assert set(filtered["expiry_str"].unique()) == {"22AUG24"}
        # Sanity: (trade_date, minute, strike, option_type) is now unique
        assert not filtered.duplicated(subset=["trade_date", "minute", "strike", "option_type"]).any()

    def test_entry_on_last_row_does_not_crash(self):
        """Fix #2 edge: entry on the literal last df row → last_same_day stays
        None → must return entry as exit, not NameError."""
        from ml_pipeline_2.scripts.rules_pipeline.execution_sim import simulate_trades

        df = _build_one_day_df(minutes=[555, 556, 557], ce_closes=[100, 101, 102])
        df.loc[2, "signal"] = True  # signal on the last row

        rule = Rule(
            rule_id="X3",
            direction="BUY_ATM_CE",
            entry_conditions=(Condition("ce_close", ">", 0),),
            disqualifiers=(),
            exit_mechanical=ExitConfig(
                stop_pct=99, target_pct=99,
                time_stop_minutes=999, eod_force_close_minute=999,
            ),
        )
        trades = simulate_trades(df, rule, exit_mode="mechanical")
        assert len(trades) == 1
        assert trades.iloc[0]["exit_reason"] == "eod_force"
        assert int(trades.iloc[0]["exit_minute"]) == 557
        assert float(trades.iloc[0]["exit_premium"]) == 102.0
