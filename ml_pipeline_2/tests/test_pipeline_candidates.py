"""Regression tests for ml_pipeline_2.pipeline.candidates -- fixtures
reconstruct the ACTUAL 2026-09-09 NIFTY label-target study result shape,
where the contaminated 0.25% config posted the best raw return of the
whole grid, to prove select_best_candidate can't be fooled the way a
human skimming a results table was almost fooled."""
from __future__ import annotations

from ml_pipeline_2.pipeline.candidates import (
    CandidateConfig,
    expand_label_grid,
    select_best_candidate,
)


def _backtest_record(candidate_id: str, *, verdict: str, return_on_capital_pct, is_thin_sample: bool, live_trades: int = 20) -> dict:
    return {
        "instrument": "NIFTY",
        "study_id": "label_target_2026-09-09",
        "node": "backtest",
        "config": {"candidate_id": candidate_id, "model_path": f"/models/{candidate_id}.joblib", "threshold": 0.5},
        "result": {
            "return_on_capital_pct": return_on_capital_pct,
            "live_trades": live_trades,
            "is_thin_sample": is_thin_sample,
        },
        "verdict": verdict,
    }


class TestExpandLabelGrid:
    def test_builds_one_candidate_per_pct(self) -> None:
        candidates = expand_label_grid("NIFTY", [0.10, 0.15, 0.20, 0.25])
        assert len(candidates) == 4
        assert all(isinstance(c, CandidateConfig) for c in candidates)
        assert candidates[0].label_pct == 0.10
        assert candidates[0].algorithm == "xgboost"

    def test_candidate_ids_are_distinct_and_readable(self) -> None:
        candidates = expand_label_grid("NIFTY", [0.10, 0.25])
        ids = [c.candidate_id for c in candidates]
        assert len(set(ids)) == 2
        assert "NIFTY" in ids[0]


class TestSelectBestCandidate:
    def test_excludes_the_contaminated_config_even_though_it_had_the_best_raw_return(self) -> None:
        # Reconstructs the actual NIFTY 2026-09-09 grid: Config E (0.25%
        # target) posted +3.09% / 69% win / 26 trades -- the best of the
        # whole study -- but was later confirmed contaminated (re-scored
        # its own training data; collapsed to 0 trades on a clean window).
        records = [
            _backtest_record("NIFTY_010pct", verdict="usable", return_on_capital_pct=0.83, is_thin_sample=False),
            _backtest_record("NIFTY_015pct", verdict="usable", return_on_capital_pct=0.57, is_thin_sample=False),
            _backtest_record("NIFTY_020pct", verdict="usable", return_on_capital_pct=0.40, is_thin_sample=False),
            _backtest_record("NIFTY_025pct", verdict="contaminated", return_on_capital_pct=3.09, is_thin_sample=False),
        ]
        result = select_best_candidate(records)
        assert result.decision == "decided"
        assert result.winner_candidate_id == "NIFTY_010pct"
        excluded = [r for r in result.ranked if r.excluded]
        assert len(excluded) == 1
        assert excluded[0].candidate_id == "NIFTY_025pct"
        assert "contaminated" in excluded[0].exclusion_reason

    def test_banknifty_010pct_wins_cleanly_against_degenerate_020_and_025(self) -> None:
        # Reconstructs the actual BankNifty 2026-09-08 study: 0.10%@thr=0.3
        # beat the live model on every metric; 0.20%/0.25% were confirmed
        # structurally degenerate.
        records = [
            _backtest_record("BANKNIFTY_010pct", verdict="usable", return_on_capital_pct=0.83, is_thin_sample=False, live_trades=87),
            _backtest_record("BANKNIFTY_015pct_live", verdict="usable", return_on_capital_pct=0.57, is_thin_sample=False, live_trades=79),
            _backtest_record("BANKNIFTY_020pct", verdict="degenerate", return_on_capital_pct=None, is_thin_sample=None, live_trades=0),
            _backtest_record("BANKNIFTY_025pct", verdict="degenerate", return_on_capital_pct=None, is_thin_sample=None, live_trades=0),
        ]
        result = select_best_candidate(records)
        assert result.decision == "decided"
        assert result.winner_candidate_id == "BANKNIFTY_010pct"

    def test_thin_sample_candidate_never_wins_over_a_robust_one_even_with_a_better_number(self) -> None:
        records = [
            _backtest_record("robust_but_modest", verdict="usable", return_on_capital_pct=0.30, is_thin_sample=False),
            _backtest_record("thin_but_flashy", verdict="usable", return_on_capital_pct=5.0, is_thin_sample=True, live_trades=3),
        ]
        result = select_best_candidate(records)
        assert result.decision == "decided"
        assert result.winner_candidate_id == "robust_but_modest"

    def test_all_thin_gives_inconclusive_not_decided(self) -> None:
        records = [
            _backtest_record("a", verdict="usable", return_on_capital_pct=0.30, is_thin_sample=True, live_trades=2),
            _backtest_record("b", verdict="usable", return_on_capital_pct=0.10, is_thin_sample=True, live_trades=1),
        ]
        result = select_best_candidate(records)
        assert result.decision == "inconclusive"
        assert result.winner_candidate_id == "a"

    def test_everything_excluded_gives_no_usable_candidates(self) -> None:
        records = [
            _backtest_record("a", verdict="degenerate", return_on_capital_pct=None, is_thin_sample=None, live_trades=0),
            _backtest_record("b", verdict="contaminated", return_on_capital_pct=9.0, is_thin_sample=False),
        ]
        result = select_best_candidate(records)
        assert result.decision == "no_usable_candidates"
        assert result.winner_candidate_id is None

    def test_empty_records_gives_no_usable_candidates(self) -> None:
        result = select_best_candidate([])
        assert result.decision == "no_usable_candidates"
        assert result.ranked == []

    def test_custom_metric_is_respected(self) -> None:
        records = [
            {
                "config": {"candidate_id": "a"},
                "result": {"win_rate_pct": 40.0, "is_thin_sample": False},
                "verdict": "usable",
            },
            {
                "config": {"candidate_id": "b"},
                "result": {"win_rate_pct": 60.0, "is_thin_sample": False},
                "verdict": "usable",
            },
        ]
        result = select_best_candidate(records, metric="win_rate_pct")
        assert result.winner_candidate_id == "b"
