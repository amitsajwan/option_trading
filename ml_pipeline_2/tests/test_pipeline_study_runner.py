"""Regression tests for ml_pipeline_2.pipeline.study_runner -- the
unattended multi-instrument orchestrator. train_fn/backtest_fn are
injected fakes throughout (no real XGBoost training, no real Mongo,
no real VM) so every isolation/sequencing/summary behavior is testable
in milliseconds."""
from __future__ import annotations

from typing import Any

import pytest

from ml_pipeline_2.pipeline.study_runner import (
    CandidatePlan,
    InstrumentStudyPlan,
    _best_threshold_from_holdout_eval,
    format_summary,
    run_all_instruments,
    run_candidate,
    run_instrument_study,
    shrink_plan_for_dry_run,
)


class FakeRegistry:
    """In-memory stand-in for RunRegistry -- same .record()/.history() shape."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def record(self, document: dict[str, Any]) -> str:
        self.docs.append(document)
        return f"fake-id-{len(self.docs)}"

    def history(self, *, instrument=None, study_id=None, node=None, limit: int = 100) -> list[dict[str, Any]]:
        out = self.docs
        if instrument:
            out = [d for d in out if d["instrument"] == instrument.upper()]
        if study_id:
            out = [d for d in out if d["study_id"] == study_id]
        if node:
            out = [d for d in out if d["node"] == node]
        return out[:limit]


def _candidate_plan(candidate_id: str, label_pct: float) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=candidate_id, label_pct=label_pct,
        train_start="2024-11-01", train_end="2026-06-15",
        valid_start="2026-06-16", valid_end="2026-06-30",
        holdout_start="2026-07-01", holdout_end="2026-07-31",
        backtest_from="2026-08-03", backtest_to="2026-09-03",
        n_trials=5,
    )


_USABLE_HOLDOUT_EVAL = {
    "separation_table": [
        {"thr": 0.3, "fire_rate": 0.04, "precision_fired": 0.53, "separation": 0.30},
        {"thr": 0.5, "fire_rate": 0.01, "precision_fired": 0.80, "separation": 0.60},
    ],
}
_DEGENERATE_HOLDOUT_EVAL = {
    "separation_table": [
        {"thr": 0.3, "degenerate": True},
        {"thr": 0.5, "degenerate": True},
    ],
}


class TestBestThresholdFromHoldoutEval:
    def test_prefers_the_higher_precision_threshold_over_raw_max_separation(self) -> None:
        # The actual 2026-09-09 SENSEX_010pct separation_table (7-threshold
        # grid, before it was widened past 0.70). thr=0.30 has the single
        # highest separation (0.2145) but fires on 63% of ALL holdout bars
        # -- barely above the 29.5% base rate, not a real entry criterion.
        # Among the genuinely selective thresholds, 0.65 has the highest
        # precision_fired (45.35%, edging out 0.70's 44.71%) -- that's
        # what should actually get picked, by precision, not separation
        # or raw threshold height.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.30, "fire_rate": 0.6306, "precision_fired": 0.3742, "base_not_fired": 0.1597, "separation": 0.2145},
                {"thr": 0.40, "fire_rate": 0.1698, "precision_fired": 0.4243, "base_not_fired": 0.2685, "separation": 0.1558},
                {"thr": 0.50, "fire_rate": 0.0800, "precision_fired": 0.4039, "base_not_fired": 0.2855, "separation": 0.1184},
                {"thr": 0.55, "fire_rate": 0.0358, "precision_fired": 0.4239, "base_not_fired": 0.2902, "separation": 0.1337},
                {"thr": 0.60, "fire_rate": 0.0354, "precision_fired": 0.4286, "base_not_fired": 0.2901, "separation": 0.1385},
                {"thr": 0.65, "fire_rate": 0.0167, "precision_fired": 0.4535, "base_not_fired": 0.2923, "separation": 0.1612},
                {"thr": 0.70, "fire_rate": 0.0165, "precision_fired": 0.4471, "base_not_fired": 0.2924, "separation": 0.1546},
            ],
        }
        assert _best_threshold_from_holdout_eval(holdout_eval) == 0.65

    def test_does_not_pick_a_worse_threshold_just_because_it_is_higher(self) -> None:
        # The real reason "highest usable threshold" (the first fix) was
        # itself wrong: NIFTY_015pct's real re-scanned table (after the
        # grid was widened to 0.95) shows precision DECREASING above 0.70
        # -- 84.6% at 0.55-0.65 down to 66.7% at 0.95. Picking "highest"
        # would grab the worst, noisiest tail. Must pick 0.55 (or any of
        # the tied 0.55-0.65 rows), not 0.95.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.30, "fire_rate": 0.1027, "precision_fired": 0.2678, "fired_count": 758, "separation": 0.183},
                {"thr": 0.55, "fire_rate": 0.0053, "precision_fired": 0.8462, "fired_count": 39, "separation": 0.7465},
                {"thr": 0.70, "fire_rate": 0.0049, "precision_fired": 0.8333, "fired_count": 36, "separation": 0.7333},
                {"thr": 0.85, "fire_rate": 0.0026, "precision_fired": 0.7368, "fired_count": 19, "separation": 0.6349},
                {"thr": 0.95, "fire_rate": 0.0016, "precision_fired": 0.6667, "fired_count": 12, "separation": 0.564},
            ],
        }
        # thr=0.85/0.95 also fall below the min_fired_count=20 floor, so
        # this simultaneously exercises both fixes.
        assert _best_threshold_from_holdout_eval(holdout_eval) == 0.55

    def test_excludes_a_high_precision_row_with_too_few_fired_holdout_rows(self) -> None:
        # BANKNIFTY_015pct's real re-scanned table: thr>=0.80 shows 100%
        # precision but only ~10-21 fired holdout rows out of 7,383 --
        # indistinguishable from noise. thr=0.70 (87.1% precision, real
        # fired_count) must win instead.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.50, "fire_rate": 0.0165, "precision_fired": 0.8033, "fired_count": 122, "separation": 0.5849},
                {"thr": 0.70, "fire_rate": 0.0042, "precision_fired": 0.871, "fired_count": 31, "separation": 0.6456},
                {"thr": 0.80, "fire_rate": 0.0028, "precision_fired": 1.0, "fired_count": 19, "separation": 0.7741},
                {"thr": 0.90, "fire_rate": 0.0014, "precision_fired": 1.0, "fired_count": 10, "separation": 0.773},
            ],
        }
        assert _best_threshold_from_holdout_eval(holdout_eval) == 0.70

    def test_still_picks_the_max_separation_threshold_when_it_is_also_most_selective(self) -> None:
        # BANKNIFTY_010pct's real table: thr=0.70 happens to have BOTH the
        # highest separation AND the highest (most selective) threshold --
        # the fix must not change this case.
        holdout_eval = {
            "separation_table": [
                {"thr": 0.30, "fire_rate": 0.9652, "precision_fired": 0.4841, "separation": 0.1923},
                {"thr": 0.50, "fire_rate": 0.6608, "precision_fired": 0.5468, "separation": 0.2046},
                {"thr": 0.65, "fire_rate": 0.2223, "precision_fired": 0.6551, "separation": 0.2284},
                {"thr": 0.70, "fire_rate": 0.0263, "precision_fired": 0.8454, "separation": 0.3778},
            ],
        }
        assert _best_threshold_from_holdout_eval(holdout_eval) == 0.70

    def test_returns_none_when_nothing_clears_the_usability_bar(self) -> None:
        holdout_eval = {"separation_table": [{"thr": 0.5, "degenerate": True}]}
        assert _best_threshold_from_holdout_eval(holdout_eval) is None


class TestShrinkPlanForDryRun:
    def test_windows_are_clipped_to_max_days_each_chained_from_train_start(self) -> None:
        plan = InstrumentStudyPlan(
            instrument="NIFTY", study_id="s1", data_dir="/d", output_dir="/o",
            candidates=[_candidate_plan("NIFTY_010pct", 0.10)],
        )
        shrunk = shrink_plan_for_dry_run(plan, max_days=5, n_trials=3)
        c = shrunk.candidates[0]
        assert c.train_start == "2024-11-01"
        assert c.train_end == "2024-11-06"
        assert c.valid_start == "2024-11-07"
        assert c.valid_end == "2024-11-12"
        assert c.holdout_start == "2024-11-13"
        assert c.holdout_end == "2024-11-18"
        assert c.backtest_from == "2024-11-19"
        assert c.backtest_to == "2024-11-24"
        assert c.n_trials == 3

    def test_original_plan_and_far_future_holdout_end_are_untouched(self) -> None:
        # Anchored at train_start, not holdout_end -- safe even when the
        # original candidate's holdout_end is near "today".
        original = _candidate_plan("NIFTY_010pct", 0.10)
        plan = InstrumentStudyPlan(instrument="NIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                    candidates=[original])
        shrink_plan_for_dry_run(plan, max_days=5, n_trials=3)
        assert plan.candidates[0] is original  # frozen dataclass, shrink returns a copy
        assert original.holdout_end == "2026-07-31"

    def test_shrinks_every_candidate_in_a_multi_candidate_plan(self) -> None:
        plan = InstrumentStudyPlan(
            instrument="NIFTY", study_id="s1", data_dir="/d", output_dir="/o",
            candidates=[_candidate_plan("NIFTY_010pct", 0.10), _candidate_plan("NIFTY_025pct", 0.25)],
        )
        shrunk = shrink_plan_for_dry_run(plan, max_days=2, n_trials=2)
        assert len(shrunk.candidates) == 2
        assert all(c.n_trials == 2 for c in shrunk.candidates)
        assert all(c.train_end == "2024-11-03" for c in shrunk.candidates)


class TestRunCandidate:
    def test_happy_path_trains_backtests_and_records_both(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates": {"auc>=0.62": True}, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            assert kwargs["entry_min_prob"] == 0.5  # picked the highest-separation threshold
            return {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.8, "win_rate_pct": 55.0}

        outcome = run_candidate(
            _candidate_plan("BN_010pct", 0.10), instrument="BANKNIFTY", study_id="s1",
            data_dir="/data", output_dir="/out", train_fn=fake_train, backtest_fn=fake_backtest, registry=registry,
        )
        assert outcome.stage_reached == "backtest_recorded"
        assert outcome.threshold_used == 0.5
        assert outcome.error is None
        assert len(registry.docs) == 2
        assert registry.docs[0]["node"] == "train"
        assert registry.docs[1]["node"] == "backtest"

    def test_training_exception_is_captured_not_raised(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs):
            raise RuntimeError("out of memory")

        outcome = run_candidate(
            _candidate_plan("BN_010pct", 0.10), instrument="BANKNIFTY", study_id="s1",
            data_dir="/data", output_dir="/out", train_fn=fake_train, backtest_fn=lambda **k: {}, registry=registry,
        )
        assert outcome.stage_reached == "train_failed"
        assert "out of memory" in outcome.error
        assert registry.docs == []

    def test_degenerate_model_skips_backtest_entirely(self) -> None:
        registry = FakeRegistry()
        backtest_called = []

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _DEGENERATE_HOLDOUT_EVAL, "ship_gates_all_pass": False}

        def fake_backtest(**kwargs) -> dict:
            backtest_called.append(True)
            return {}

        outcome = run_candidate(
            _candidate_plan("BN_025pct", 0.25), instrument="BANKNIFTY", study_id="s1",
            data_dir="/data", output_dir="/out", train_fn=fake_train, backtest_fn=fake_backtest, registry=registry,
        )
        assert outcome.stage_reached == "trained_degenerate"
        assert not backtest_called  # never wastes a backtest on a provably-degenerate model
        assert len(registry.docs) == 1  # train record only
        assert registry.docs[0]["verdict"] == "degenerate"

    def test_backtest_exception_is_captured_train_record_still_kept(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs):
            raise RuntimeError("VM disk full")

        outcome = run_candidate(
            _candidate_plan("BN_010pct", 0.10), instrument="BANKNIFTY", study_id="s1",
            data_dir="/data", output_dir="/out", train_fn=fake_train, backtest_fn=fake_backtest, registry=registry,
        )
        assert outcome.stage_reached == "backtest_failed"
        assert "VM disk full" in outcome.error
        assert len(registry.docs) == 1
        assert registry.docs[0]["node"] == "train"


class TestRunInstrumentStudy:
    def test_multiple_candidates_run_sequentially_and_winner_is_selected(self) -> None:
        registry = FakeRegistry()
        call_order: list[str] = []

        def fake_train(**kwargs) -> dict:
            call_order.append(f"train:{kwargs['label_pct']}")
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            call_order.append(f"backtest:{kwargs['model_path']}")
            # 0.10% candidate does best, matching this session's real BankNifty result shape
            return_pct = 0.83 if "010" in kwargs["model_path"] else 0.40
            return {"live_trades": 30, "is_thin_sample": False, "return_on_capital_pct": return_pct}

        plan = InstrumentStudyPlan(
            instrument="BANKNIFTY", study_id="overnight_1", data_dir="/data", output_dir="/out",
            candidates=[_candidate_plan("BANKNIFTY_010pct", 0.10), _candidate_plan("BANKNIFTY_015pct", 0.15)],
        )
        outcome = run_instrument_study(plan, train_fn=fake_train, backtest_fn=fake_backtest, registry=registry)

        assert call_order == [
            "train:0.1", "backtest:/out/BANKNIFTY_010pct.joblib",
            "train:0.15", "backtest:/out/BANKNIFTY_015pct.joblib",
        ]
        assert len(outcome.candidates) == 2
        assert outcome.selection is not None
        assert outcome.selection.decision == "decided"
        assert outcome.selection.winner_candidate_id == "BANKNIFTY_010pct"
        deploy_decisions = registry.history(instrument="BANKNIFTY", study_id="overnight_1", node="deploy_decision")
        assert len(deploy_decisions) == 1

    def test_one_failed_candidate_does_not_stop_the_rest(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            if kwargs["label_pct"] == 0.20:
                raise RuntimeError("bad data for this candidate")
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            return {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.5}

        plan = InstrumentStudyPlan(
            instrument="NIFTY", study_id="overnight_1", data_dir="/data", output_dir="/out",
            candidates=[
                _candidate_plan("NIFTY_010pct", 0.10),
                _candidate_plan("NIFTY_020pct", 0.20),  # fails
                _candidate_plan("NIFTY_025pct", 0.25),
            ],
        )
        outcome = run_instrument_study(plan, train_fn=fake_train, backtest_fn=fake_backtest, registry=registry)

        assert [c.stage_reached for c in outcome.candidates] == [
            "backtest_recorded", "train_failed", "backtest_recorded",
        ]
        assert outcome.fatal_error is None
        assert outcome.selection is not None
        assert outcome.selection.decision == "decided"  # the 2 survivors still produce a real decision

    def test_no_candidates_produce_backtests_gives_no_selection_not_a_crash(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _DEGENERATE_HOLDOUT_EVAL, "ship_gates_all_pass": False}

        plan = InstrumentStudyPlan(
            instrument="FINNIFTY", study_id="overnight_1", data_dir="/data", output_dir="/out",
            candidates=[_candidate_plan("FINNIFTY_015pct", 0.15)],
        )
        outcome = run_instrument_study(plan, train_fn=fake_train, backtest_fn=lambda **k: {}, registry=registry)
        assert outcome.selection is None
        assert outcome.fatal_error is None


class TestRunAllInstruments:
    def test_a_candidate_level_training_failure_does_not_stop_other_instruments(self) -> None:
        # Two layers of isolation: run_candidate already catches a training
        # exception as stage_reached="train_failed" on that ONE candidate --
        # it never even reaches instrument-level fatal_error. Confirms the
        # other instruments in the run are completely unaffected either way.
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            if kwargs["instrument"] == "FINNIFTY":
                raise RuntimeError("data_dir missing for FINNIFTY")
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            return {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.5}

        plans = [
            InstrumentStudyPlan(instrument="BANKNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("BN_010pct", 0.10)]),
            InstrumentStudyPlan(instrument="FINNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("FN_010pct", 0.10)]),
            InstrumentStudyPlan(instrument="SENSEX", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("SX_010pct", 0.10)]),
        ]
        outcomes = run_all_instruments(plans, train_fn=fake_train, backtest_fn=fake_backtest, registry=registry)

        assert len(outcomes) == 3
        assert outcomes[0].fatal_error is None and outcomes[0].selection.decision == "decided"
        assert outcomes[1].instrument == "FINNIFTY" and outcomes[1].fatal_error is None
        assert outcomes[1].candidates[0].stage_reached == "train_failed"
        assert outcomes[1].selection is None  # no backtest records -> nothing to select
        assert outcomes[2].fatal_error is None and outcomes[2].selection.decision == "decided"

    def test_an_instrument_level_fatal_error_does_not_stop_the_next_instrument(self) -> None:
        # A failure OUTSIDE the per-candidate try/except (e.g. a broken
        # registry query at selection time) -- this is what fatal_error is
        # actually for, one layer up from a single candidate's own failure.
        class BrokenHistoryRegistry(FakeRegistry):
            def history(self, *, instrument=None, study_id=None, node=None, limit: int = 100):
                if instrument == "FINNIFTY":
                    raise RuntimeError("mongo connection reset")
                return super().history(instrument=instrument, study_id=study_id, node=node, limit=limit)

        registry = BrokenHistoryRegistry()

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            return {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.5}

        plans = [
            InstrumentStudyPlan(instrument="BANKNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("BN_010pct", 0.10)]),
            InstrumentStudyPlan(instrument="FINNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("FN_010pct", 0.10)]),
            InstrumentStudyPlan(instrument="SENSEX", study_id="s1", data_dir="/d", output_dir="/o",
                                 candidates=[_candidate_plan("SX_010pct", 0.10)]),
        ]
        outcomes = run_all_instruments(plans, train_fn=fake_train, backtest_fn=fake_backtest, registry=registry)

        assert outcomes[0].fatal_error is None and outcomes[0].selection.decision == "decided"
        assert outcomes[1].instrument == "FINNIFTY"
        assert outcomes[1].fatal_error is not None and "mongo connection reset" in outcomes[1].fatal_error
        assert outcomes[2].fatal_error is None and outcomes[2].selection.decision == "decided"


class TestFormatSummary:
    def test_summary_mentions_every_instrument_and_the_winner(self) -> None:
        registry = FakeRegistry()

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        def fake_backtest(**kwargs) -> dict:
            return {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.8, "win_rate_pct": 55.0}

        plans = [InstrumentStudyPlan(instrument="BANKNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                      candidates=[_candidate_plan("BN_010pct", 0.10)])]
        outcomes = run_all_instruments(plans, train_fn=fake_train, backtest_fn=fake_backtest, registry=registry)
        summary = format_summary(outcomes)
        assert "BANKNIFTY" in summary
        assert "BN_010pct" in summary
        assert "DECISION: decided" in summary

    def test_summary_surfaces_a_fatal_error_clearly(self) -> None:
        class BrokenHistoryRegistry(FakeRegistry):
            def history(self, **kwargs):
                raise RuntimeError("mongo connection reset")

        def fake_train(**kwargs) -> dict:
            return {"holdout_eval": _USABLE_HOLDOUT_EVAL, "ship_gates_all_pass": True}

        outcomes = run_all_instruments(
            [InstrumentStudyPlan(instrument="FINNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                  candidates=[_candidate_plan("FN_010pct", 0.10)])],
            train_fn=fake_train,
            backtest_fn=lambda **k: {"live_trades": 20, "is_thin_sample": False, "return_on_capital_pct": 0.5},
            registry=BrokenHistoryRegistry(),
        )
        summary = format_summary(outcomes)
        assert "FATAL" in summary
        assert "mongo connection reset" in summary

    def test_summary_shows_a_candidate_training_failure_inline_without_a_fatal_marker(self) -> None:
        outcomes = run_all_instruments(
            [InstrumentStudyPlan(instrument="FINNIFTY", study_id="s1", data_dir="/d", output_dir="/o",
                                  candidates=[_candidate_plan("FN_010pct", 0.10)])],
            train_fn=lambda **k: (_ for _ in ()).throw(RuntimeError("no data")),
            backtest_fn=lambda **k: {}, registry=FakeRegistry(),
        )
        summary = format_summary(outcomes)
        assert "FATAL" not in summary
        assert "train_failed" in summary
        assert "no data" in summary
