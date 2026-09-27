"""Tests for ml_pipeline_2.pipeline.joint_direction -- all pure functions,
synthetic fixtures only (no model, no snapshot parquet needed). Fixture
convention matches test_direction_margin.py (this track's closest sibling)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ml_pipeline_2.pipeline.joint_direction import (
    CALL,
    CLASS_NAMES,
    CLASS_TO_INT,
    INT_TO_CLASS,
    NO_TRADE,
    PUT,
    argmax_class,
    class_actual_value,
    class_balance,
    class_decile_eval,
    compare_top_decile_to_baseline,
    compute_no_trade_ship_gates,
    compute_three_class_label,
    decode_label,
    encode_label,
    multiclass_diagnostics,
    top_decile_mask_within_argmax,
)


class TestComputeThreeClassLabel:
    def test_call_when_ce_beats_pe_both_positive(self) -> None:
        out = compute_three_class_label([0.10], [0.02])
        assert list(out) == [CALL]

    def test_put_when_pe_beats_ce_both_positive(self) -> None:
        out = compute_three_class_label([0.02], [0.10])
        assert list(out) == [PUT]

    def test_no_trade_when_neither_side_clears_cost(self) -> None:
        out = compute_three_class_label([-0.01], [-0.05])
        assert list(out) == [NO_TRADE]

    def test_no_trade_when_max_is_exactly_zero(self) -> None:
        out = compute_three_class_label([0.0], [-0.02])
        assert list(out) == [NO_TRADE]

    def test_tie_breaks_to_put(self) -> None:
        # net_ce_return == net_pe_return, both positive -- "CALL if ce > pe
        # else PUT" means an exact tie goes to PUT, documented, deterministic.
        out = compute_three_class_label([0.05], [0.05])
        assert list(out) == [PUT]

    def test_none_when_either_leg_missing(self) -> None:
        out = compute_three_class_label([0.10, None, 0.05], [None, 0.02, 0.01])
        assert out[0] is None
        assert out[1] is None
        assert out[2] == CALL

    def test_matches_real_class_balance_expectation(self) -> None:
        # Real 2026-09-27 measurement: CALL~41.9%, PUT~41.3%, NO_TRADE~16.8%
        # over 136,261 both-legs-usable rows -- this synthetic check just
        # confirms NO_TRADE is the MINORITY class under a realistic mix,
        # not the specific real percentages.
        rng = np.random.default_rng(7)
        n = 5000
        ce = rng.normal(loc=0.01, scale=0.05, size=n)
        pe = rng.normal(loc=0.01, scale=0.05, size=n)
        out = compute_three_class_label(ce, pe)
        counts = pd.Series(out).value_counts()
        assert counts[NO_TRADE] < counts[CALL]
        assert counts[NO_TRADE] < counts[PUT]


class TestEncodeDecodeLabel:
    def test_round_trip(self) -> None:
        labels = [NO_TRADE, CALL, PUT, CALL]
        ints = encode_label(labels)
        assert list(ints) == [0, 1, 2, 1]
        assert list(decode_label(ints)) == labels

    def test_encode_raises_on_unknown_value(self) -> None:
        with pytest.raises(ValueError):
            encode_label([CALL, "BOGUS"])

    def test_encode_raises_on_none(self) -> None:
        with pytest.raises(ValueError):
            encode_label([CALL, None])

    def test_decode_raises_on_unknown_int(self) -> None:
        with pytest.raises(ValueError):
            decode_label([0, 1, 99])

    def test_class_to_int_and_int_to_class_are_inverses(self) -> None:
        for cls, i in CLASS_TO_INT.items():
            assert INT_TO_CLASS[i] == cls
        assert set(CLASS_TO_INT) == set(CLASS_NAMES)


class TestClassBalance:
    def test_counts_and_fractions(self) -> None:
        labels = [CALL, CALL, PUT, NO_TRADE, None]
        report = class_balance(labels)
        assert report["n_total"] == 5
        assert report["n_resolved"] == 4
        assert report["n_unresolved"] == 1
        assert report["counts"] == {NO_TRADE: 1, CALL: 2, PUT: 1}
        assert report["fractions"][CALL] == pytest.approx(0.5)
        assert report["fractions"][PUT] == pytest.approx(0.25)
        assert report["fractions"][NO_TRADE] == pytest.approx(0.25)

    def test_all_unresolved(self) -> None:
        report = class_balance([None, None])
        assert report["n_resolved"] == 0
        assert report["fractions"][CALL] is None


class TestArgmaxClass:
    def test_picks_highest_probability_column(self) -> None:
        # Columns ordered per CLASS_TO_INT: [NO_TRADE, CALL, PUT]
        proba = np.array([
            [0.7, 0.2, 0.1],   # NO_TRADE
            [0.1, 0.8, 0.1],   # CALL
            [0.1, 0.1, 0.8],   # PUT
        ])
        out = argmax_class(proba)
        assert list(out) == [NO_TRADE, CALL, PUT]

    def test_raises_on_wrong_shape(self) -> None:
        with pytest.raises(ValueError):
            argmax_class(np.array([[0.5, 0.5]]))


class TestClassActualValue:
    def test_call_returns_ce(self) -> None:
        out = class_actual_value([0.05, -0.02], [0.01, 0.03], target_class=CALL)
        assert list(out) == pytest.approx([0.05, -0.02])

    def test_put_returns_pe(self) -> None:
        out = class_actual_value([0.05, -0.02], [0.01, 0.03], target_class=PUT)
        assert list(out) == pytest.approx([0.01, 0.03])

    def test_no_trade_returns_elementwise_max(self) -> None:
        out = class_actual_value([0.05, -0.02], [0.01, 0.03], target_class=NO_TRADE)
        assert list(out) == pytest.approx([0.05, 0.03])

    def test_nan_when_either_leg_missing(self) -> None:
        out = class_actual_value([0.05, None], [None, 0.03], target_class=NO_TRADE)
        assert math.isnan(out[0])
        assert math.isnan(out[1])

    def test_raises_on_unknown_class(self) -> None:
        with pytest.raises(ValueError):
            class_actual_value([0.1], [0.1], target_class="BOGUS")

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            class_actual_value([0.1, 0.2], [0.1], target_class=CALL)


class TestTopDecileMaskWithinArgmax:
    def test_selects_top_fraction_of_argmax_only(self) -> None:
        # 10 CALL-argmax bars (proba 0..9), 10 PUT-argmax bars (proba 100..109,
        # deliberately higher, must be ignored since they're not CALL-argmax).
        proba = np.concatenate([np.arange(10.0), np.arange(100.0, 110.0)])
        argmax = np.array([CALL] * 10 + [PUT] * 10, dtype=object)
        mask = top_decile_mask_within_argmax(proba, argmax, target_class=CALL, frac=0.2)
        assert mask.sum() == 2  # top 20% of the 10 CALL-argmax bars
        selected_proba = proba[mask]
        assert set(selected_proba) == {8.0, 9.0}  # highest two among the CALL subset only
        assert not mask[10:].any()  # never touches the PUT-argmax bars

    def test_empty_when_no_bars_have_that_argmax(self) -> None:
        proba = np.array([0.1, 0.2, 0.3])
        argmax = np.array([PUT, PUT, NO_TRADE], dtype=object)
        mask = top_decile_mask_within_argmax(proba, argmax, target_class=CALL, frac=0.5)
        assert mask.sum() == 0

    def test_at_least_one_selected_even_with_tiny_frac(self) -> None:
        proba = np.array([0.1, 0.2, 0.3])
        argmax = np.array([CALL, CALL, CALL], dtype=object)
        mask = top_decile_mask_within_argmax(proba, argmax, target_class=CALL, frac=0.01)
        assert mask.sum() == 1
        assert proba[mask][0] == pytest.approx(0.3)  # the single highest

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            top_decile_mask_within_argmax([0.1, 0.2], [CALL], target_class=CALL)


class TestClassDecileEval:
    def test_perfect_rank_correlation_within_argmax_subset(self) -> None:
        n = 100
        proba = np.arange(n, dtype=float)
        actual = np.arange(n, dtype=float) * 0.01
        argmax = np.array([CALL] * n, dtype=object)
        result = class_decile_eval(proba, actual, argmax, target_class=CALL)
        assert result["n_argmax"] == n
        assert result["rows"] == n
        assert result["spearman_rho"] == pytest.approx(1.0, abs=1e-6)
        assert len(result["decile_table"]) == 10
        assert result["top_decile_mean"] > result["bottom_decile_mean"]

    def test_only_argmax_subset_is_scored(self) -> None:
        # 5 CALL-argmax bars with a real positive relationship, 5 PUT-argmax
        # bars with a deliberately inverted one -- CALL's eval must be blind
        # to the PUT-argmax bars entirely.
        proba = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0])
        actual = np.array([0.01, 0.02, 0.03, 0.04, 0.05, 5.0, 4.0, 3.0, 2.0, 1.0])
        argmax = np.array([CALL] * 5 + [PUT] * 5, dtype=object)
        result = class_decile_eval(proba, actual, argmax, target_class=CALL)
        assert result["n_argmax"] == 5
        assert result["rows"] == 5
        assert result["spearman_rho"] == pytest.approx(1.0, abs=1e-6)

    def test_nan_actual_rows_excluded_from_rows_but_not_n_argmax(self) -> None:
        proba = np.array([1.0, 2.0, 3.0, 4.0])
        actual = np.array([0.1, np.nan, 0.3, np.nan])
        argmax = np.array([CALL] * 4, dtype=object)
        result = class_decile_eval(proba, actual, argmax, target_class=CALL)
        assert result["n_argmax"] == 4
        assert result["rows"] == 2

    def test_no_argmax_bars_returns_none_stats_not_a_crash(self) -> None:
        proba = np.array([0.1, 0.2])
        actual = np.array([0.01, 0.02])
        argmax = np.array([PUT, PUT], dtype=object)
        result = class_decile_eval(proba, actual, argmax, target_class=CALL)
        assert result["n_argmax"] == 0
        assert result["rows"] == 0
        assert result["spearman_rho"] is None
        assert result["decile_table"] == []

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            class_decile_eval([0.1, 0.2], [0.1], [CALL, CALL], target_class=CALL)


class TestComputeNoTradeShipGates:
    def _eval(self, **overrides) -> dict:
        base = {"rows": 500, "spearman_rho": -0.10, "spearman_p_value": 0.001, "top_decile_mean": -0.02, "bottom_decile_mean": 0.03}
        base.update(overrides)
        return base

    def test_passes_when_confidently_no_trade_correlates_with_low_return(self) -> None:
        gates = compute_no_trade_ship_gates(self._eval())
        assert all(gates.values())

    def test_fails_when_rho_is_positive(self) -> None:
        gates = compute_no_trade_ship_gates(self._eval(spearman_rho=0.10))
        assert gates["spearman_rho<=-0.05"] is False

    def test_fails_when_top_decile_not_below_bottom_decile(self) -> None:
        # Inverted -- most-confident NO_TRADE bars have a HIGHER best-achievable
        # return than the least-confident ones. Not doing real work.
        gates = compute_no_trade_ship_gates(self._eval(top_decile_mean=0.05, bottom_decile_mean=-0.01))
        assert gates["top_decile_mean<bottom_decile_mean"] is False

    def test_fails_on_thin_sample(self) -> None:
        gates = compute_no_trade_ship_gates(self._eval(rows=50))
        assert gates["rows>=200"] is False


class TestCompareTopDecileToBaseline:
    def test_none_joint_mean_reports_none_verdicts(self) -> None:
        out = compare_top_decile_to_baseline(None, -0.00547)
        assert out["beats_baseline"] is None
        assert out["crosses_to_positive"] is None

    def test_beats_baseline_but_still_negative(self) -> None:
        out = compare_top_decile_to_baseline(-0.001, -0.00547)
        assert out["beats_baseline"] is True
        assert out["crosses_to_positive"] is False

    def test_crosses_to_positive(self) -> None:
        out = compare_top_decile_to_baseline(0.01, -0.00547)
        assert out["beats_baseline"] is True
        assert out["crosses_to_positive"] is True

    def test_worse_than_baseline(self) -> None:
        out = compare_top_decile_to_baseline(-0.02, -0.00547)
        assert out["beats_baseline"] is False
        assert out["crosses_to_positive"] is False


class TestMulticlassDiagnostics:
    def test_perfect_predictions(self) -> None:
        labels = [NO_TRADE, CALL, PUT, CALL, PUT]
        result = multiclass_diagnostics(labels, labels)
        assert result["accuracy"] == pytest.approx(1.0)
        assert result["macro_f1"] == pytest.approx(1.0)
        assert result["weighted_f1"] == pytest.approx(1.0)
        assert result["labels"] == list(CLASS_NAMES)

    def test_confusion_matrix_is_always_3x3_even_if_a_class_is_absent(self) -> None:
        true_labels = [CALL, CALL, PUT, PUT]  # no NO_TRADE at all in this slice
        pred_labels = [CALL, PUT, PUT, PUT]
        result = multiclass_diagnostics(true_labels, pred_labels)
        cm = np.array(result["confusion_matrix"])
        assert cm.shape == (3, 3)
        no_trade_idx = list(CLASS_NAMES).index(NO_TRADE)
        assert cm[no_trade_idx, :].sum() == 0  # no true NO_TRADE rows
        assert result["per_class"][NO_TRADE]["support"] == 0

    def test_per_class_precision_recall(self) -> None:
        true_labels = [CALL, CALL, PUT, PUT]
        pred_labels = [CALL, PUT, PUT, PUT]  # one CALL misclassified as PUT
        result = multiclass_diagnostics(true_labels, pred_labels)
        assert result["per_class"][CALL]["recall"] == pytest.approx(0.5)
        assert result["per_class"][CALL]["precision"] == pytest.approx(1.0)
        assert result["per_class"][PUT]["recall"] == pytest.approx(1.0)
        assert result["per_class"][PUT]["precision"] == pytest.approx(2 / 3, abs=1e-4)

    def test_raises_on_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            multiclass_diagnostics([CALL, PUT], [CALL])
