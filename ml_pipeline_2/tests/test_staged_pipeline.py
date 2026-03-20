from __future__ import annotations

import pandas as pd

from ml_pipeline_2.contracts.types import LabelRecipe
from ml_pipeline_2.staged import pipeline as staged_pipeline
from ml_pipeline_2.staged.registries import view_registry


def test_build_oracle_targets_aligns_recipe_rows_by_key(monkeypatch) -> None:
    support = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "snap_b",
            },
        ]
    )

    def _fake_label_recipe_frame(_support: pd.DataFrame, _recipe: LabelRecipe) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "trade_date": "2024-01-01",
                    "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                    "snapshot_id": "snap_b",
                    "ce_label_valid": 1.0,
                    "pe_label_valid": 1.0,
                    "ce_path_exit_reason": "time_stop",
                    "pe_path_exit_reason": "time_stop",
                    "ce_barrier_upper_return": 0.01,
                    "pe_barrier_upper_return": 0.01,
                    "ce_barrier_lower_return": 0.005,
                    "pe_barrier_lower_return": 0.005,
                    "ce_forward_return": 0.0,
                    "pe_forward_return": 0.0,
                    "ce_mae": -0.001,
                    "pe_mae": -0.001,
                },
                {
                    "trade_date": "2024-01-01",
                    "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                    "snapshot_id": "snap_a",
                    "ce_label_valid": 1.0,
                    "pe_label_valid": 1.0,
                    "ce_path_exit_reason": "tp",
                    "pe_path_exit_reason": "time_stop",
                    "ce_barrier_upper_return": 0.01,
                    "pe_barrier_upper_return": 0.01,
                    "ce_barrier_lower_return": 0.005,
                    "pe_barrier_lower_return": 0.005,
                    "ce_forward_return": 0.0,
                    "pe_forward_return": 0.0,
                    "ce_mae": -0.001,
                    "pe_mae": -0.001,
                },
            ]
        )

    monkeypatch.setattr(staged_pipeline, "_label_recipe_frame", _fake_label_recipe_frame)

    oracle, utility = staged_pipeline._build_oracle_targets(  # type: ignore[attr-defined]
        support,
        [LabelRecipe(recipe_id="L0", horizon_minutes=15, take_profit_pct=0.0025, stop_loss_pct=0.0008)],
        cost_per_trade=0.0006,
    )

    assert oracle.loc[oracle["snapshot_id"] == "snap_a", "entry_label"].iloc[0] == 1
    assert oracle.loc[oracle["snapshot_id"] == "snap_a", "direction_label"].iloc[0] == "CE"
    assert oracle.loc[oracle["snapshot_id"] == "snap_b", "entry_label"].iloc[0] == 0
    assert utility.loc[utility["snapshot_id"] == "snap_a", "best_available_net_return_after_cost"].iloc[0] > 0.0


def test_build_stage2_labels_drops_invalid_direction_rows() -> None:
    stage_frame = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "snap_b",
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:17:00"),
                "snapshot_id": "snap_c",
            },
        ]
    )
    oracle = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
                "entry_label": 1,
                "direction_label": "CE",
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "snap_b",
                "entry_label": 1,
                "direction_label": "PE",
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:17:00"),
                "snapshot_id": "snap_c",
                "entry_label": 1,
                "direction_label": None,
            },
        ]
    )

    labeled = staged_pipeline.build_stage2_labels(stage_frame, oracle)

    assert labeled["snapshot_id"].tolist() == ["snap_a", "snap_b"]
    assert labeled["move_first_hit_side"].tolist() == ["up", "down"]


def test_view_registry_is_cached() -> None:
    assert view_registry() is view_registry()


def test_add_upstream_probs_scores_on_source_stage_views(monkeypatch) -> None:
    target = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
                "atr_ratio": 1.1,
            }
        ]
    )
    stage1_source = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
                "ema_9_slope": 0.02,
            }
        ]
    )
    stage2_source = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "snap_a",
                "ema_21_slope": 0.03,
            }
        ]
    )
    calls: list[tuple[str, list[str]]] = []

    def _fake_score(frame: pd.DataFrame, _package: dict[str, object], *, prob_col: str) -> pd.DataFrame:
        calls.append((prob_col, list(frame.columns)))
        out = frame.loc[:, staged_pipeline.KEY_COLUMNS].copy()
        out[prob_col] = 0.7 if prob_col == "stage1_entry_prob" else 0.4
        return out

    monkeypatch.setattr(staged_pipeline, "_score_single_target", _fake_score)

    out = staged_pipeline._add_upstream_probs(
        target,
        stage1_source_frame=stage1_source,
        stage2_source_frame=stage2_source,
        stage1_package={},
        stage2_package={},
    )

    assert out["stage1_entry_prob"].tolist() == [0.7]
    assert out["stage2_direction_up_prob"].tolist() == [0.4]
    assert out["stage2_direction_down_prob"].tolist() == [0.6]
    assert calls == [
        ("stage1_entry_prob", ["trade_date", "timestamp", "snapshot_id", "ema_9_slope"]),
        ("stage2_direction_up_prob", ["trade_date", "timestamp", "snapshot_id", "ema_21_slope"]),
    ]
