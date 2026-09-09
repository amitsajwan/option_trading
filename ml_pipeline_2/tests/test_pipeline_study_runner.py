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
