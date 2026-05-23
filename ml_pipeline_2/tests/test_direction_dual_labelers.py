"""Tests for ce_win_v1 and pe_win_v1 per-side direction labelers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _make_oracle(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _make_stage_frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": ["2024-01-02"] * n,
        "timestamp": pd.date_range("2024-01-02 10:00", periods=n, freq="1min"),
        "snapshot_id": [f"s{i}" for i in range(n)],
        "feat_a": np.random.rand(n),
    })


KEY = ["trade_date", "timestamp", "snapshot_id"]


def _oracle_row(i: int, ce_ret: float, pe_ret: float) -> dict:
    return {
        "trade_date": "2024-01-02",
        "timestamp": pd.Timestamp(f"2024-01-02 10:0{i}:00"),
        "snapshot_id": f"s{i}",
        "best_ce_net_return_after_cost": ce_ret,
        "best_pe_net_return_after_cost": pe_ret,
    }


class TestCeWinV1:
    def test_positive_class_when_ce_profitable(self):
        from ml_pipeline_2.staged.pipeline import build_stage2_labels_ce_win_v1

        sf = _make_stage_frame(3)
        oracle = pd.DataFrame([
            _oracle_row(0, ce_ret=0.01, pe_ret=-0.005),   # CE win → "CE"
            _oracle_row(1, ce_ret=-0.008, pe_ret=0.01),   # CE loss → "PE"
            _oracle_row(2, ce_ret=0.001, pe_ret=0.002),   # |ce_ret| < 0.003 → excluded
        ])
        manifest = {"training": {"stage2_decisive_move_filter": {"min_abs_return": 0.003}}}
        result = build_stage2_labels_ce_win_v1(sf, oracle, manifest)
        assert len(result) == 2
        assert result.iloc[0]["direction_label"] == "CE"
        assert result.iloc[1]["direction_label"] == "PE"

    def test_ambiguous_rows_excluded(self):
        from ml_pipeline_2.staged.pipeline import build_stage2_labels_ce_win_v1

        sf = _make_stage_frame(2)
        oracle = pd.DataFrame([
            _oracle_row(0, ce_ret=0.001, pe_ret=-0.005),  # |ce_ret| = 0.001 < 0.003 → excluded
            _oracle_row(1, ce_ret=-0.001, pe_ret=0.005),  # |ce_ret| = 0.001 < 0.003 → excluded
        ])
        result = build_stage2_labels_ce_win_v1(sf, oracle)
        assert len(result) == 0

    def test_move_label_valid_is_one(self):
        from ml_pipeline_2.staged.pipeline import build_stage2_labels_ce_win_v1

        sf = _make_stage_frame(1)
        oracle = pd.DataFrame([_oracle_row(0, ce_ret=0.01, pe_ret=-0.005)])
        result = build_stage2_labels_ce_win_v1(sf, oracle)
        assert result.iloc[0]["move_label_valid"] == 1.0
        assert result.iloc[0]["move_label"] == 1.0


class TestPeWinV1:
    def test_positive_class_when_pe_profitable(self):
        from ml_pipeline_2.staged.pipeline import build_stage2_labels_pe_win_v1

        sf = _make_stage_frame(2)
        oracle = pd.DataFrame([
            _oracle_row(0, ce_ret=0.005, pe_ret=0.01),   # PE win → "CE" (positive class)
            _oracle_row(1, ce_ret=0.005, pe_ret=-0.008), # PE loss → "PE" (negative class)
        ])
        result = build_stage2_labels_pe_win_v1(sf, oracle)
        assert len(result) == 2
        assert result.iloc[0]["direction_label"] == "CE"
        assert result.iloc[1]["direction_label"] == "PE"

    def test_default_min_edge(self):
        from ml_pipeline_2.staged.pipeline import build_stage2_labels_pe_win_v1

        sf = _make_stage_frame(1)
        oracle = pd.DataFrame([_oracle_row(0, ce_ret=0.005, pe_ret=0.001)])
        # |pe_ret| = 0.001 < default 0.003 → excluded
        result = build_stage2_labels_pe_win_v1(sf, oracle)
        assert len(result) == 0

    def test_registered_in_registry(self):
        from ml_pipeline_2.staged.registries import label_registry, resolve_labeler

        assert "ce_win_v1" in label_registry()
        assert "pe_win_v1" in label_registry()
        assert label_registry()["ce_win_v1"] == "stage2"
        assert label_registry()["pe_win_v1"] == "stage2"
        # should not raise
        resolve_labeler("ce_win_v1")
        resolve_labeler("pe_win_v1")
