from __future__ import annotations

import numpy as np
import pandas as pd

from ml_pipeline_2.contracts.types import LabelRecipe
from ml_pipeline_2.staged import pipeline as staged_pipeline
from ml_pipeline_2.staged.registries import view_registry


def _legacy_build_oracle_targets(
    support: pd.DataFrame,
    recipes: list[LabelRecipe],
    *,
    cost_per_trade: float,
) -> pd.DataFrame:
    utility = support.loc[:, staged_pipeline.KEY_COLUMNS].copy()
    recipe_rows_by_key: dict[str, dict[tuple[str, pd.Timestamp, str], dict[str, object]]] = {}
    for recipe in recipes:
        labeled = staged_pipeline._align_recipe_frame(  # type: ignore[attr-defined]
            support,
            staged_pipeline._label_recipe_frame(support, recipe),  # type: ignore[attr-defined]
            recipe_id=recipe.recipe_id,
        )
        recipe_rows_by_key[recipe.recipe_id] = {
            (str(row["trade_date"]), pd.Timestamp(row["timestamp"]), str(row["snapshot_id"])): dict(row)
            for row in labeled.to_dict(orient="records")
        }
        utility[f"{recipe.recipe_id}__ce_net_return"] = labeled.apply(
            lambda row: staged_pipeline._path_return(row, prefix="ce") - float(cost_per_trade),  # type: ignore[attr-defined]
            axis=1,
        )
        utility[f"{recipe.recipe_id}__pe_net_return"] = labeled.apply(
            lambda row: staged_pipeline._path_return(row, prefix="pe") - float(cost_per_trade),  # type: ignore[attr-defined]
            axis=1,
        )

    best_ce_cols = [f"{recipe.recipe_id}__ce_net_return" for recipe in recipes]
    best_pe_cols = [f"{recipe.recipe_id}__pe_net_return" for recipe in recipes]
    utility["best_ce_net_return_after_cost"] = utility[best_ce_cols].max(axis=1)
    utility["best_pe_net_return_after_cost"] = utility[best_pe_cols].max(axis=1)
    utility["best_available_net_return_after_cost"] = utility[
        ["best_ce_net_return_after_cost", "best_pe_net_return_after_cost"]
    ].max(axis=1)
    utility_by_key = {
        (str(row["trade_date"]), pd.Timestamp(row["timestamp"]), str(row["snapshot_id"])): dict(row)
        for row in utility.to_dict(orient="records")
    }

    rows: list[dict[str, object]] = []
    for support_row in support.to_dict(orient="records"):
        key = (str(support_row["trade_date"]), pd.Timestamp(support_row["timestamp"]), str(support_row["snapshot_id"]))
        utility_row = utility_by_key[key]
        best: dict[str, object] | None = None
        for recipe in recipes:
            row = recipe_rows_by_key[recipe.recipe_id][key]
            for side, prefix, direction_up in (("CE", "ce", 1), ("PE", "pe", 0)):
                valid = staged_pipeline._safe_float(row.get(f"{prefix}_label_valid"), default=0.0)  # type: ignore[attr-defined]
                net = staged_pipeline._safe_float(utility_row[f"{recipe.recipe_id}__{prefix}_net_return"])  # type: ignore[attr-defined]
                if valid != 1.0 or (not np.isfinite(net)) or net <= 0.0:
                    continue
                candidate = {
                    "recipe_id": recipe.recipe_id,
                    "side": side,
                    "direction_up": int(direction_up),
                    "net_return_after_cost": float(net),
                    "adverse_excursion": float(staged_pipeline._adverse_excursion(row, prefix=prefix)),  # type: ignore[attr-defined]
                    "horizon_minutes": int(recipe.horizon_minutes),
                    "stop_loss_pct": float(recipe.stop_loss_pct),
                    "take_profit_pct": float(recipe.take_profit_pct),
                }
                if staged_pipeline._candidate_better(candidate, best):  # type: ignore[attr-defined]
                    best = candidate
        rows.append(
            {
                "trade_date": str(support_row["trade_date"]),
                "timestamp": pd.Timestamp(support_row["timestamp"]),
                "snapshot_id": str(support_row["snapshot_id"]),
                "entry_label": int(best is not None),
                "direction_label": (str(best["side"]) if best is not None else None),
                "direction_up": (int(best["direction_up"]) if best is not None else None),
                "recipe_label": (str(best["recipe_id"]) if best is not None else None),
                "best_net_return_after_cost": (
                    float(best["net_return_after_cost"])
                    if best is not None
                    else float(utility_row["best_available_net_return_after_cost"])
                ),
            }
        )
    return pd.DataFrame(rows)


def _policy_fixture() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    key_rows = [
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
        {
            "trade_date": "2024-01-01",
            "timestamp": pd.Timestamp("2024-01-01 09:18:00"),
            "snapshot_id": "snap_d",
        },
    ]
    utility = pd.DataFrame(
        [
            {
                **key_rows[0],
                "best_available_net_return_after_cost": 0.010,
                "best_ce_net_return_after_cost": 0.010,
                "best_pe_net_return_after_cost": -0.004,
                "L0__ce_net_return": 0.010,
                "L0__pe_net_return": -0.004,
                "L1__ce_net_return": 0.008,
                "L1__pe_net_return": -0.003,
            },
            {
                **key_rows[1],
                "best_available_net_return_after_cost": 0.009,
                "best_ce_net_return_after_cost": -0.002,
                "best_pe_net_return_after_cost": 0.009,
                "L0__ce_net_return": -0.002,
                "L0__pe_net_return": 0.007,
                "L1__ce_net_return": -0.003,
                "L1__pe_net_return": 0.009,
            },
            {
                **key_rows[2],
                "best_available_net_return_after_cost": 0.006,
                "best_ce_net_return_after_cost": 0.006,
                "best_pe_net_return_after_cost": 0.005,
                "L0__ce_net_return": 0.006,
                "L0__pe_net_return": 0.005,
                "L1__ce_net_return": 0.004,
                "L1__pe_net_return": 0.003,
            },
            {
                **key_rows[3],
                "best_available_net_return_after_cost": -0.001,
                "best_ce_net_return_after_cost": -0.001,
                "best_pe_net_return_after_cost": -0.002,
                "L0__ce_net_return": -0.001,
                "L0__pe_net_return": -0.002,
                "L1__ce_net_return": -0.0015,
                "L1__pe_net_return": -0.0025,
            },
        ]
    )
    stage1_scores = pd.DataFrame(
        [
            {**key_rows[0], "entry_prob": 0.80},
            {**key_rows[1], "entry_prob": 0.74},
            {**key_rows[2], "entry_prob": 0.58},
            {**key_rows[3], "entry_prob": 0.42},
        ]
    )
    stage2_scores = pd.DataFrame(
        [
            {**key_rows[0], "direction_up_prob": 0.78},
            {**key_rows[1], "direction_up_prob": 0.26},
            {**key_rows[2], "direction_up_prob": 0.51},
            {**key_rows[3], "direction_up_prob": 0.60},
        ]
    )
    stage3_scores = pd.DataFrame(
        [
            {**key_rows[0], "recipe_prob_L0": 0.62, "recipe_prob_L1": 0.55},
            {**key_rows[1], "recipe_prob_L0": 0.54, "recipe_prob_L1": 0.61},
            {**key_rows[2], "recipe_prob_L0": 0.56, "recipe_prob_L1": 0.56},
            {**key_rows[3], "recipe_prob_L0": 0.40, "recipe_prob_L1": 0.39},
        ]
    )
    return utility, stage1_scores, stage2_scores, stage3_scores


def _legacy_select_direction_policy(
    valid_scores: pd.DataFrame,
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage1_policy: dict[str, object],
    policy_config: dict[str, object],
) -> dict[str, object]:
    merged = valid_scores.merge(stage1_scores, on=staged_pipeline.KEY_COLUMNS, how="inner").merge(
        utility, on=staged_pipeline.KEY_COLUMNS, how="inner"
    )
    entry_threshold = float(stage1_policy["selected_threshold"])
    rows: list[dict[str, object]] = []
    for ce_threshold in list(policy_config.get("ce_threshold_grid") or []):
        for pe_threshold in list(policy_config.get("pe_threshold_grid") or []):
            for min_edge in list(policy_config.get("min_edge_grid") or []):
                returns: list[float] = []
                sides: list[str] = []
                for row in merged.itertuples(index=False):
                    data = row._asdict()
                    if staged_pipeline._safe_float(data.get("entry_prob"), default=0.0) < entry_threshold:  # type: ignore[attr-defined]
                        continue
                    side = staged_pipeline._choose_side(  # type: ignore[attr-defined]
                        staged_pipeline._safe_float(data.get("direction_up_prob"), default=0.0),  # type: ignore[attr-defined]
                        ce_threshold=float(ce_threshold),
                        pe_threshold=float(pe_threshold),
                        min_edge=float(min_edge),
                    )
                    if side is None:
                        continue
                    returns.append(
                        staged_pipeline._safe_float(  # type: ignore[attr-defined]
                            data.get("best_ce_net_return_after_cost" if side == "CE" else "best_pe_net_return_after_cost"),
                            default=0.0,
                        )
                    )
                    sides.append(side)
                summary = staged_pipeline._summarize_returns(returns, rows_total=len(merged), sides=sides)  # type: ignore[attr-defined]
                summary.update(
                    {
                        "ce_threshold": float(ce_threshold),
                        "pe_threshold": float(pe_threshold),
                        "min_edge": float(min_edge),
                    }
                )
                rows.append(summary)
    best = max(
        rows,
        key=lambda row: (
            float(row["net_return_sum"]),
            float(row["profit_factor"]),
            int(row["trades"]),
            -float(row["min_edge"]),
        ),
    )
    return {
        "policy_id": "direction_dual_threshold_v1",
        "selected_ce_threshold": float(best["ce_threshold"]),
        "selected_pe_threshold": float(best["pe_threshold"]),
        "selected_min_edge": float(best["min_edge"]),
        "validation_rows": rows,
        "selected_validation_summary": best,
    }


def _legacy_select_entry_policy(
    valid_scores: pd.DataFrame,
    utility: pd.DataFrame,
    policy_config: dict[str, object],
) -> dict[str, object]:
    merged = valid_scores.merge(utility, on=staged_pipeline.KEY_COLUMNS, how="inner")
    rows: list[dict[str, object]] = []
    for threshold in list(policy_config.get("threshold_grid") or []):
        mask = pd.to_numeric(merged["entry_prob"], errors="coerce").fillna(0.0) >= float(threshold)
        returns = (
            pd.to_numeric(merged.loc[mask, "best_available_net_return_after_cost"], errors="coerce")
            .fillna(0.0)
            .tolist()
        )
        summary = staged_pipeline._summarize_returns(returns, rows_total=len(merged))  # type: ignore[attr-defined]
        summary["threshold"] = float(threshold)
        rows.append(summary)
    best = max(
        rows,
        key=lambda row: (
            float(row["net_return_sum"]),
            float(row["profit_factor"]),
            int(row["trades"]),
            -float(row["threshold"]),
        ),
    )
    return {
        "policy_id": "entry_threshold_v1",
        "selected_threshold": float(best["threshold"]),
        "validation_rows": rows,
        "selected_validation_summary": best,
    }


def _legacy_evaluate_combined_policy(
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    stage3_scores: pd.DataFrame,
    *,
    stage1_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
    recipe_threshold: float,
    recipe_margin_min: float,
    recipe_ids: list[str],
) -> dict[str, object]:
    merged = utility.merge(stage1_scores, on=staged_pipeline.KEY_COLUMNS, how="inner")
    merged = merged.merge(stage2_scores, on=staged_pipeline.KEY_COLUMNS, how="inner")
    merged = merged.merge(stage3_scores, on=staged_pipeline.KEY_COLUMNS, how="inner")
    returns: list[float] = []
    sides: list[str] = []
    recipes: list[str] = []
    for row in merged.itertuples(index=False):
        data = row._asdict()
        if staged_pipeline._safe_float(data.get("entry_prob"), default=0.0) < float(stage1_threshold):  # type: ignore[attr-defined]
            continue
        side = staged_pipeline._choose_side(  # type: ignore[attr-defined]
            staged_pipeline._safe_float(data.get("direction_up_prob"), default=0.0),  # type: ignore[attr-defined]
            ce_threshold=float(ce_threshold),
            pe_threshold=float(pe_threshold),
            min_edge=float(min_edge),
        )
        if side is None:
            continue
        recipe_id = staged_pipeline._choose_recipe(  # type: ignore[attr-defined]
            data,
            recipe_ids,
            threshold=float(recipe_threshold),
            margin_min=float(recipe_margin_min),
        )
        if recipe_id is None:
            continue
        returns.append(staged_pipeline._safe_float(data.get(f"{recipe_id}__{side.lower()}_net_return"), default=0.0))  # type: ignore[attr-defined]
        sides.append(side)
        recipes.append(recipe_id)
    summary = staged_pipeline._summarize_returns(returns, rows_total=len(merged), sides=sides, selected_recipes=recipes)  # type: ignore[attr-defined]
    summary["recipe_threshold"] = float(recipe_threshold)
    summary["recipe_margin_min"] = float(recipe_margin_min)
    return summary


def _legacy_fixed_recipe_baseline(
    utility: pd.DataFrame,
    stage1_scores: pd.DataFrame,
    stage2_scores: pd.DataFrame,
    *,
    stage1_threshold: float,
    ce_threshold: float,
    pe_threshold: float,
    min_edge: float,
    recipe_id: str,
) -> dict[str, object]:
    merged = utility.merge(stage1_scores, on=staged_pipeline.KEY_COLUMNS, how="inner").merge(
        stage2_scores, on=staged_pipeline.KEY_COLUMNS, how="inner"
    )
    returns: list[float] = []
    sides: list[str] = []
    for row in merged.itertuples(index=False):
        data = row._asdict()
        if staged_pipeline._safe_float(data.get("entry_prob"), default=0.0) < float(stage1_threshold):  # type: ignore[attr-defined]
            continue
        side = staged_pipeline._choose_side(  # type: ignore[attr-defined]
            staged_pipeline._safe_float(data.get("direction_up_prob"), default=0.0),  # type: ignore[attr-defined]
            ce_threshold=float(ce_threshold),
            pe_threshold=float(pe_threshold),
            min_edge=float(min_edge),
        )
        if side is None:
            continue
        returns.append(staged_pipeline._safe_float(data.get(f"{recipe_id}__{side.lower()}_net_return"), default=0.0))  # type: ignore[attr-defined]
        sides.append(side)
    summary = staged_pipeline._summarize_returns(  # type: ignore[attr-defined]
        returns,
        rows_total=len(merged),
        sides=sides,
        selected_recipes=[recipe_id] * len(returns),
    )
    summary["recipe_id"] = recipe_id
    return summary


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


def test_build_oracle_targets_matches_legacy_candidate_selection(monkeypatch) -> None:
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
    recipes = [
        LabelRecipe(recipe_id="L0", horizon_minutes=15, take_profit_pct=0.0025, stop_loss_pct=0.0010),
        LabelRecipe(recipe_id="L1", horizon_minutes=15, take_profit_pct=0.0025, stop_loss_pct=0.0010),
    ]

    def _fake_label_recipe_frame(_support: pd.DataFrame, recipe: LabelRecipe) -> pd.DataFrame:
        if recipe.recipe_id == "L0":
            return pd.DataFrame(
                [
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
                        "pe_forward_return": -0.002,
                        "ce_mae": -0.003,
                        "pe_mae": -0.001,
                    },
                    {
                        "trade_date": "2024-01-01",
                        "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                        "snapshot_id": "snap_b",
                        "ce_label_valid": 1.0,
                        "pe_label_valid": 1.0,
                        "ce_path_exit_reason": "time_stop",
                        "pe_path_exit_reason": "tp",
                        "ce_barrier_upper_return": 0.008,
                        "pe_barrier_upper_return": 0.009,
                        "ce_barrier_lower_return": 0.004,
                        "pe_barrier_lower_return": 0.004,
                        "ce_forward_return": -0.001,
                        "pe_forward_return": 0.0,
                        "ce_mae": -0.001,
                        "pe_mae": -0.002,
                    },
                ]
            )
        return pd.DataFrame(
            [
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
                    "pe_forward_return": -0.003,
                    "ce_mae": -0.001,
                    "pe_mae": -0.001,
                },
                {
                    "trade_date": "2024-01-01",
                    "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                    "snapshot_id": "snap_b",
                    "ce_label_valid": 1.0,
                    "pe_label_valid": 1.0,
                    "ce_path_exit_reason": "tp",
                    "pe_path_exit_reason": "tp",
                    "ce_barrier_upper_return": 0.008,
                    "pe_barrier_upper_return": 0.009,
                    "ce_barrier_lower_return": 0.004,
                    "pe_barrier_lower_return": 0.004,
                    "ce_forward_return": 0.0,
                    "pe_forward_return": 0.0,
                    "ce_mae": -0.002,
                    "pe_mae": -0.001,
                },
            ]
        )

    monkeypatch.setattr(staged_pipeline, "_label_recipe_frame", _fake_label_recipe_frame)

    oracle, _utility = staged_pipeline._build_oracle_targets(  # type: ignore[attr-defined]
        support,
        recipes,
        cost_per_trade=0.0006,
    )
    expected = _legacy_build_oracle_targets(support, recipes, cost_per_trade=0.0006)

    pd.testing.assert_frame_equal(oracle[expected.columns], expected)


def test_build_oracle_targets_masks_invalid_recipe_returns_in_utility(monkeypatch) -> None:
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
                    "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                    "snapshot_id": "snap_a",
                    "ce_label_valid": 0.0,
                    "pe_label_valid": 0.0,
                    "ce_path_exit_reason": "tp",
                    "pe_path_exit_reason": "tp",
                    "ce_barrier_upper_return": 0.010,
                    "pe_barrier_upper_return": 0.011,
                    "ce_barrier_lower_return": 0.004,
                    "pe_barrier_lower_return": 0.004,
                    "ce_forward_return": 0.0,
                    "pe_forward_return": 0.0,
                    "ce_mae": -0.001,
                    "pe_mae": -0.001,
                },
                {
                    "trade_date": "2024-01-01",
                    "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                    "snapshot_id": "snap_b",
                    "ce_label_valid": 0.0,
                    "pe_label_valid": 1.0,
                    "ce_path_exit_reason": "tp",
                    "pe_path_exit_reason": "tp",
                    "ce_barrier_upper_return": 0.012,
                    "pe_barrier_upper_return": 0.009,
                    "ce_barrier_lower_return": 0.004,
                    "pe_barrier_lower_return": 0.004,
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

    snap_a = utility.loc[utility["snapshot_id"] == "snap_a"].iloc[0]
    snap_b = utility.loc[utility["snapshot_id"] == "snap_b"].iloc[0]
    assert pd.isna(snap_a["L0__ce_net_return"])
    assert pd.isna(snap_a["L0__pe_net_return"])
    assert pd.isna(snap_a["best_available_net_return_after_cost"])
    assert pd.isna(snap_b["L0__ce_net_return"])
    assert float(snap_b["best_pe_net_return_after_cost"]) > 0.0
    assert oracle.loc[oracle["snapshot_id"] == "snap_a", "entry_label"].iloc[0] == 0
    assert oracle.loc[oracle["snapshot_id"] == "snap_b", "direction_label"].iloc[0] == "PE"


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


def test_build_stage2_labels_direction_or_no_trade_marks_abstains_from_target_redesign() -> None:
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
                "best_ce_net_return_after_cost": 0.0030,
                "best_pe_net_return_after_cost": -0.0003,
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "snap_b",
                "entry_label": 1,
                "direction_label": "PE",
                "best_ce_net_return_after_cost": 0.0004,
                "best_pe_net_return_after_cost": 0.0011,
            },
        ]
    )

    labeled = staged_pipeline.build_stage2_labels_direction_or_no_trade(
        stage_frame,
        oracle,
        {
            "training": {
                "stage2_target_redesign": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.0018,
                    "min_winner_return_after_cost": 0.0010,
                    "max_opposing_return_after_cost": -0.0002,
                    "max_kept_fraction": 1.0,
                    "conviction_score": "edge_winner_min",
                }
            }
        },
    )

    assert labeled["snapshot_id"].tolist() == ["snap_a", "snap_b"]
    assert labeled["move_label"].tolist() == [1.0, 0.0]
    assert labeled["move_first_hit_side"].tolist() == ["up", "down"]


def test_apply_stage2_label_filter_can_enforce_valid_winner_after_cost() -> None:
    stage2_frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "timestamp": pd.to_datetime(
                ["2024-01-02 09:16:00", "2024-01-02 09:17:00", "2024-01-02 09:18:00"]
            ),
            "snapshot_id": ["s1", "s2", "s3"],
            "direction_label": ["CE", "CE", "PE"],
            "best_ce_net_return_after_cost": [0.0015, 0.0020, -0.0004],
            "best_pe_net_return_after_cost": [0.0008, 0.0002, 0.0005],
            "direction_return_edge_after_cost": [0.0007, 0.0018, 0.0009],
        }
    )

    filtered, meta = staged_pipeline._apply_stage2_label_filter(
        stage2_frame,
        {
            "training": {
                "stage2_label_filter": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.0005,
                    "require_positive_winner_after_cost": True,
                    "max_opposing_return_after_cost": 0.0,
                }
            }
        },
    )

    assert filtered["snapshot_id"].tolist() == ["s3"]
    assert meta["rows_before"] == 3
    assert meta["rows_after"] == 1
    assert meta["valid_winner_rows_dropped"] == 2


def test_apply_stage2_session_filter_keeps_only_requested_buckets() -> None:
    stage2_frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-02"] * 4,
            "timestamp": pd.to_datetime(
                [
                    "2024-01-02 09:20:00",
                    "2024-01-02 10:30:00",
                    "2024-01-02 12:10:00",
                    "2024-01-02 14:10:00",
                ]
            ),
            "snapshot_id": ["s1", "s2", "s3", "s4"],
            "direction_label": ["CE", "PE", "CE", "PE"],
        }
    )

    filtered, meta = staged_pipeline._apply_stage2_session_filter(
        stage2_frame,
        {
            "training": {
                "stage2_session_filter": {
                    "enabled": True,
                    "include_buckets": ["MIDDAY", "MORNING"],
                }
            }
        },
    )

    assert filtered["snapshot_id"].tolist() == ["s2", "s3"]
    assert meta["rows_before"] == 4
    assert meta["rows_after"] == 2
    assert meta["rows_dropped"] == 2
    assert meta["observed_buckets_after"] == ["MIDDAY", "MORNING"]


def test_build_stage2_split_diagnostics_reports_balanced_scores_and_buckets() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-01"] * 8,
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 09:20:00",
                    "2024-01-01 09:45:00",
                    "2024-01-01 10:30:00",
                    "2024-01-01 11:45:00",
                    "2024-01-01 12:15:00",
                    "2024-01-01 13:00:00",
                    "2024-01-01 14:00:00",
                    "2024-01-01 14:45:00",
                ]
            ),
            "snapshot_id": [f"s{idx}" for idx in range(8)],
            "direction_label": ["CE", "PE", "CE", "PE", "CE", "PE", "CE", "PE"],
            "direction_return_edge_after_cost": [0.0010, 0.0011, 0.0012, 0.0013, 0.0014, 0.0015, 0.0016, 0.0017],
            "ctx_is_expiry_day": [1, 1, 0, 0, 0, 0, 0, 0],
            "ctx_regime_expiry_near": [0, 0, 1, 1, 0, 0, 0, 0],
        }
    )
    scores = frame.loc[:, staged_pipeline.KEY_COLUMNS].copy()
    scores["direction_up_prob"] = [0.88, 0.22, 0.76, 0.28, 0.67, 0.31, 0.73, 0.19]

    report = staged_pipeline._build_stage2_split_diagnostics(
        frame,
        scores,
        split_name="research_valid",
    )

    assert report["split"] == "research_valid"
    assert report["rows"] == 8
    assert report["positive_rate"] == 0.5
    assert len(report["probability_histogram"]["bins"]) == 10
    assert report["score_separation"]["positive_mean_prob"] > report["score_separation"]["negative_mean_prob"]
    assert report["calibration"]["calibration_error"] is not None
    assert {row["group"] for row in report["quality_by_expiry_regime"]} >= {"EXPIRY_DAY", "NEAR_EXPIRY", "REGULAR"}
    assert {row["group"] for row in report["quality_by_time_bucket"]} >= {"OPENING", "MORNING", "MIDDAY", "LATE_SESSION"}


def test_build_stage2_split_diagnostics_handles_near_constant_probs_and_unknown_buckets() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-01"] * 4,
            "timestamp": [pd.NaT, pd.NaT, pd.NaT, pd.NaT],
            "snapshot_id": [f"k{idx}" for idx in range(4)],
            "direction_label": ["CE", "PE", "CE", "PE"],
            "direction_return_edge_after_cost": [0.0009, 0.0010, 0.0011, 0.0012],
        }
    )
    scores = frame.loc[:, staged_pipeline.KEY_COLUMNS].copy()
    scores["direction_up_prob"] = [0.5, 0.5, 0.5, 0.5]

    report = staged_pipeline._build_stage2_split_diagnostics(
        frame,
        scores,
        split_name="final_holdout",
    )

    assert report["quality"]["brier"] == 0.25
    assert report["score_separation"]["mean_gap"] == 0.0
    assert report["calibration"]["calibration_error"] == 0.0
    assert report["quality_by_time_bucket"] == [
        {"group": "UNKNOWN", "rows": 4, "positive_rate": 0.5, "roc_auc": 0.5, "brier": 0.25}
    ]
    assert report["quality_by_expiry_regime"] == [
        {"group": "REGULAR", "rows": 4, "positive_rate": 0.5, "roc_auc": 0.5, "brier": 0.25}
    ]


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


def test_stage_gate_result_fails_closed_when_metrics_are_unavailable() -> None:
    gate_ok, reasons = staged_pipeline._stage_gate_result(  # type: ignore[attr-defined]
        {
            "roc_auc": None,
            "brier": None,
            "roc_auc_drift_half_split": None,
        },
        {
            "roc_auc_min": 0.60,
            "brier_max": 0.20,
            "roc_auc_drift_half_split_max_abs": 0.10,
        },
        prefix="stage1_cv.",
    )

    assert gate_ok is False
    assert reasons == [
        "stage1_cv.roc_auc_unavailable",
        "stage1_cv.brier_unavailable",
        "stage1_cv.roc_auc_drift_unavailable",
    ]


def test_regime_label_series_treats_nan_explicit_values_as_missing() -> None:
    frame = pd.DataFrame(
        [
            {"regime": np.nan, "ctx_regime_trend_up": 1.0},
            {"regime": None, "ctx_is_expiry_day": 1.0},
            {"regime": "", "ctx_regime_atr_low": 0.0},
            {"regime": "sideways", "ctx_regime_trend_up": 1.0},
        ]
    )

    out = staged_pipeline._regime_label_series(frame)  # type: ignore[attr-defined]

    assert out.tolist() == ["TRENDING", "PRE_EXPIRY", "UNKNOWN", "SIDEWAYS"]
    assert "NAN" not in out.tolist()


def test_combined_policy_evaluation_keeps_full_rows_total_when_stage3_scores_are_sparse() -> None:
    utility, stage1_scores, stage2_scores, stage3_scores = _policy_fixture()
    sparse_stage3_scores = stage3_scores.iloc[:2].copy()

    summary = staged_pipeline._evaluate_combined_policy(  # type: ignore[attr-defined]
        utility,
        stage1_scores,
        stage2_scores,
        sparse_stage3_scores,
        stage1_threshold=0.55,
        ce_threshold=0.55,
        pe_threshold=0.55,
        min_edge=0.05,
        recipe_threshold=0.50,
        recipe_margin_min=0.05,
        recipe_ids=["L0", "L1"],
    )
    selected = staged_pipeline._combined_policy_trade_rows(  # type: ignore[attr-defined]
        utility.loc[:, staged_pipeline.KEY_COLUMNS].copy(),
        utility,
        stage1_scores,
        stage2_scores,
        sparse_stage3_scores,
        stage1_threshold=0.55,
        ce_threshold=0.55,
        pe_threshold=0.55,
        min_edge=0.05,
        recipe_threshold=0.50,
        recipe_margin_min=0.05,
        recipe_ids=["L0", "L1"],
    )

    assert summary["rows_total"] == len(utility)
    assert summary["trades"] == 2
    assert selected["snapshot_id"].tolist() == ["snap_a", "snap_b"]


def test_apply_stage2_label_filter_stricter_abstain_reduces_rows_deterministically() -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2024-01-01 12:00:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0030,
                "best_pe_net_return_after_cost": -0.0005,
                "direction_return_edge_after_cost": 0.0035,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:01:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0020,
                "best_pe_net_return_after_cost": 0.0001,
                "direction_return_edge_after_cost": 0.0019,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:02:00"),
                "direction_label": "PE",
                "best_ce_net_return_after_cost": 0.0002,
                "best_pe_net_return_after_cost": 0.0025,
                "direction_return_edge_after_cost": 0.0023,
            },
        ]
    )

    filtered, meta = staged_pipeline._apply_stage2_label_filter(  # type: ignore[attr-defined]
        frame,
        {
            "training": {
                "stage2_label_filter": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.0018,
                    "require_positive_winner_after_cost": True,
                    "max_opposing_return_after_cost": -0.0004,
                }
            }
        },
    )

    assert len(filtered) == 1
    assert filtered["direction_label"].tolist() == ["CE"]
    assert meta["rows_before"] == 3
    assert meta["rows_after"] == 1
    assert meta["rows_dropped"] == 2


def test_apply_stage2_target_redesign_reduces_ambiguous_rows() -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2024-01-01 12:00:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0030,
                "best_pe_net_return_after_cost": -0.0003,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:01:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0011,
                "best_pe_net_return_after_cost": 0.0002,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:02:00"),
                "direction_label": "PE",
                "best_ce_net_return_after_cost": -0.0003,
                "best_pe_net_return_after_cost": 0.0022,
            },
        ]
    )

    filtered, meta = staged_pipeline._apply_stage2_target_redesign(  # type: ignore[attr-defined]
        frame,
        {
            "training": {
                "stage2_target_redesign": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.0018,
                    "min_winner_return_after_cost": 0.0010,
                    "max_opposing_return_after_cost": -0.0002,
                }
            }
        },
    )

    assert len(filtered) == 2
    assert filtered["direction_label"].tolist() == ["CE", "PE"]
    assert meta["rows_before"] == 3
    assert meta["rows_after"] == 2
    assert meta["rows_dropped"] == 1


def test_apply_stage2_target_redesign_can_keep_only_high_conviction_fraction() -> None:
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2024-01-01 12:00:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0040,
                "best_pe_net_return_after_cost": -0.0004,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:01:00"),
                "direction_label": "CE",
                "best_ce_net_return_after_cost": 0.0032,
                "best_pe_net_return_after_cost": -0.0001,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:02:00"),
                "direction_label": "PE",
                "best_ce_net_return_after_cost": -0.0002,
                "best_pe_net_return_after_cost": 0.0028,
            },
            {
                "timestamp": pd.Timestamp("2024-01-01 12:03:00"),
                "direction_label": "PE",
                "best_ce_net_return_after_cost": 0.0001,
                "best_pe_net_return_after_cost": 0.0022,
            },
        ]
    )

    filtered, meta = staged_pipeline._apply_stage2_target_redesign(  # type: ignore[attr-defined]
        frame,
        {
            "training": {
                "stage2_target_redesign": {
                    "enabled": True,
                    "min_directional_edge_after_cost": 0.0018,
                    "min_winner_return_after_cost": 0.0010,
                    "max_opposing_return_after_cost": 0.0001,
                    "max_kept_fraction": 0.5,
                    "conviction_score": "edge_winner_min",
                }
            }
        },
    )

    assert len(filtered) == 2
    assert filtered["timestamp"].tolist() == [
        pd.Timestamp("2024-01-01 12:00:00"),
        pd.Timestamp("2024-01-01 12:01:00"),
    ]
    assert "direction_winner_return_after_cost" in filtered.columns
    assert "direction_target_conviction_score" in filtered.columns
    assert meta["post_threshold_rows"] == 4
    assert meta["rows_after"] == 2
    assert meta["conviction_rank_rows_dropped"] == 2
    assert meta["conviction_keep_count"] == 2
    assert meta["conviction_score_floor"] is not None


def test_select_direction_gate_policy_prefers_trade_gate_and_direction_thresholds() -> None:
    utility, stage1_scores, stage2_scores, _ = _policy_fixture()
    stage2_scores = stage2_scores.copy()
    stage2_scores["direction_trade_prob"] = [0.82, 0.76, 0.48, 0.74]
    stage1_policy = {"selected_threshold": 0.45}
    policy = staged_pipeline.select_direction_gate_policy(
        stage2_scores,
        utility,
        stage1_scores,
        stage1_policy,
        {
            "trade_threshold_grid": [0.45, 0.60],
            "ce_threshold_grid": [0.55, 0.60],
            "pe_threshold_grid": [0.55, 0.60],
            "min_edge_grid": [0.05, 0.10],
        },
    )

    assert policy["policy_id"] == "direction_gate_threshold_v1"
    assert "selected_trade_threshold" in policy
    assert policy["selected_trade_threshold"] in {0.45, 0.60}


def test_direction_gate_economic_balance_policy_prefers_balanced_non_negative_candidate() -> None:
    utility, stage1_scores, stage2_scores, _ = _policy_fixture()
    utility = utility.copy()
    utility.loc[utility["snapshot_id"].isin(["snap_c", "snap_d"]), "best_ce_net_return_after_cost"] = [-0.01, -0.01]
    stage1_scores = stage1_scores.copy()
    stage1_scores.loc[stage1_scores["snapshot_id"] == "snap_d", "entry_prob"] = 0.48
    stage2_scores = stage2_scores.copy()
    stage2_scores["direction_trade_prob"] = [0.8, 0.8, 0.8, 0.8]
    stage2_scores["direction_up_prob"] = [0.82, 0.20, 0.72, 0.74]

    policy = staged_pipeline.select_direction_gate_economic_balance_policy(
        stage2_scores,
        utility,
        stage1_scores,
        {"selected_threshold": 0.45},
        {
            "trade_threshold_grid": [0.45],
            "ce_threshold_grid": [0.55, 0.75],
            "pe_threshold_grid": [0.55],
            "min_edge_grid": [0.05],
            "validation_min_trades_soft": 2,
            "side_share_min": 0.3,
            "side_share_max": 0.7,
            "prefer_non_negative_returns": True,
            "prefer_profit_factor_min": 1.0,
        },
    )

    assert policy["policy_id"] == "direction_gate_economic_balance_v1"
    assert policy["selected_ce_threshold"] == 0.75
    assert policy["selected_validation_summary"]["side_share_in_band"] is True
    assert float(policy["selected_validation_summary"]["net_return_sum"]) > 0.0


def test_recipe_economic_balance_policy_prefers_non_negative_candidate() -> None:
    utility = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "a",
                "best_available_net_return_after_cost": 0.01,
                "best_ce_net_return_after_cost": 0.01,
                "best_pe_net_return_after_cost": -0.01,
                "L0__ce_net_return": 0.01,
                "L0__pe_net_return": -0.01,
                "L1__ce_net_return": -0.02,
                "L1__pe_net_return": -0.02,
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "b",
                "best_available_net_return_after_cost": 0.008,
                "best_ce_net_return_after_cost": -0.01,
                "best_pe_net_return_after_cost": 0.008,
                "L0__ce_net_return": -0.01,
                "L0__pe_net_return": 0.004,
                "L1__ce_net_return": -0.03,
                "L1__pe_net_return": -0.03,
            },
        ]
    )
    stage1_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "entry_prob": 0.9},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "entry_prob": 0.9},
        ]
    )
    stage2_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "direction_up_prob": 0.8},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "direction_up_prob": 0.2},
        ]
    )
    stage3_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "recipe_prob_L0": 0.70, "recipe_prob_L1": 0.55},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "recipe_prob_L0": 0.45, "recipe_prob_L1": 0.50},
        ]
    )

    policy = staged_pipeline.select_recipe_economic_balance_policy(
        stage3_scores,
        utility,
        stage1_scores,
        stage2_scores,
        {"selected_threshold": 0.45},
        {"policy_id": "direction_dual_threshold_v1", "selected_ce_threshold": 0.55, "selected_pe_threshold": 0.55, "selected_min_edge": 0.05},
        {
            "threshold_grid": [0.45, 0.65],
            "margin_grid": [0.02],
            "validation_min_trades_soft": 1,
            "side_share_min": 0.3,
            "side_share_max": 0.7,
            "prefer_non_negative_returns": True,
            "prefer_profit_factor_min": 1.0,
        },
        ["L0", "L1"],
    )

    assert policy["policy_id"] == "recipe_economic_balance_v1"
    assert policy["selected_threshold"] == 0.65
    assert float(policy["selected_validation_summary"]["net_return_sum"]) > 0.0


def test_recipe_fixed_baseline_guard_can_select_fixed_recipe_and_combined_policy_honors_it() -> None:
    utility = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:15:00"),
                "snapshot_id": "a",
                "best_available_net_return_after_cost": 0.01,
                "best_ce_net_return_after_cost": 0.01,
                "best_pe_net_return_after_cost": -0.01,
                "L0__ce_net_return": 0.01,
                "L0__pe_net_return": -0.01,
                "L1__ce_net_return": -0.02,
                "L1__pe_net_return": -0.02,
            },
            {
                "trade_date": "2024-01-01",
                "timestamp": pd.Timestamp("2024-01-01 09:16:00"),
                "snapshot_id": "b",
                "best_available_net_return_after_cost": 0.008,
                "best_ce_net_return_after_cost": -0.01,
                "best_pe_net_return_after_cost": 0.008,
                "L0__ce_net_return": -0.01,
                "L0__pe_net_return": 0.008,
                "L1__ce_net_return": -0.03,
                "L1__pe_net_return": -0.03,
            },
        ]
    )
    stage1_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "entry_prob": 0.9},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "entry_prob": 0.9},
        ]
    )
    stage2_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "direction_up_prob": 0.8},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "direction_up_prob": 0.2},
        ]
    )
    stage3_scores = pd.DataFrame(
        [
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:15:00"), "snapshot_id": "a", "recipe_prob_L0": 0.55, "recipe_prob_L1": 0.80},
            {"trade_date": "2024-01-01", "timestamp": pd.Timestamp("2024-01-01 09:16:00"), "snapshot_id": "b", "recipe_prob_L0": 0.52, "recipe_prob_L1": 0.78},
        ]
    )
    stage2_policy = {
        "policy_id": "direction_dual_threshold_v1",
        "selected_ce_threshold": 0.55,
        "selected_pe_threshold": 0.55,
        "selected_min_edge": 0.05,
    }
    stage1_policy = {"selected_threshold": 0.45}
    policy_config = {
        "threshold_grid": [0.5],
        "margin_grid": [0.02],
        "validation_min_trades_soft": 1,
        "side_share_min": 0.3,
        "side_share_max": 0.7,
        "prefer_non_negative_returns": True,
        "prefer_profit_factor_min": 1.0,
        "non_inferior_drawdown_slack": 0.01,
    }

    policy = staged_pipeline.select_recipe_fixed_baseline_guard_policy(
        stage3_scores,
        utility,
        stage1_scores,
        stage2_scores,
        stage1_policy,
        stage2_policy,
        policy_config,
        ["L0", "L1"],
    )

    summary = staged_pipeline._evaluate_combined_policy(  # type: ignore[attr-defined]
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=float(stage1_policy["selected_threshold"]),
        stage2_policy=stage2_policy,
        stage3_policy=policy,
        recipe_ids=["L0", "L1"],
    )
    trade_rows = staged_pipeline._combined_policy_trade_rows(  # type: ignore[attr-defined]
        stage3_scores.loc[:, staged_pipeline.KEY_COLUMNS].copy(),
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=float(stage1_policy["selected_threshold"]),
        stage2_policy=stage2_policy,
        stage3_policy=policy,
        recipe_ids=["L0", "L1"],
    )

    assert policy["policy_id"] == "recipe_fixed_baseline_guard_v1"
    assert policy["selection_mode"] == "fixed_recipe"
    assert policy["selected_recipe_id"] == "L0"
    assert float(summary["net_return_sum"]) > 0.0
    assert summary["selected_recipe_id"] == "L0"
    assert set(trade_rows["selected_recipe"]) == {"L0"}


def test_vectorized_policy_selection_matches_legacy_loops() -> None:
    utility, stage1_scores, stage2_scores, stage3_scores = _policy_fixture()
    stage1_policy_cfg = {"threshold_grid": [0.45, 0.55, 0.65]}
    stage2_policy_cfg = {
        "ce_threshold_grid": [0.50, 0.55, 0.60],
        "pe_threshold_grid": [0.50, 0.55, 0.60],
        "min_edge_grid": [0.0, 0.05],
    }
    stage3_policy_cfg = {"threshold_grid": [0.50, 0.55], "margin_grid": [0.0, 0.05]}
    recipe_ids = ["L0", "L1"]

    entry_policy = staged_pipeline.select_entry_policy(stage1_scores, utility, stage1_policy_cfg)
    expected_entry_policy = _legacy_select_entry_policy(stage1_scores, utility, stage1_policy_cfg)
    assert entry_policy == expected_entry_policy

    direction_policy = staged_pipeline.select_direction_policy(
        stage2_scores,
        utility,
        stage1_scores,
        entry_policy,
        stage2_policy_cfg,
    )
    expected_direction_policy = _legacy_select_direction_policy(
        stage2_scores,
        utility,
        stage1_scores,
        entry_policy,
        stage2_policy_cfg,
    )
    assert direction_policy == expected_direction_policy

    combined_summary = staged_pipeline._evaluate_combined_policy(  # type: ignore[attr-defined]
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=float(entry_policy["selected_threshold"]),
        ce_threshold=float(direction_policy["selected_ce_threshold"]),
        pe_threshold=float(direction_policy["selected_pe_threshold"]),
        min_edge=float(direction_policy["selected_min_edge"]),
        recipe_threshold=0.50,
        recipe_margin_min=0.05,
        recipe_ids=recipe_ids,
    )
    expected_combined_summary = _legacy_evaluate_combined_policy(
        utility,
        stage1_scores,
        stage2_scores,
        stage3_scores,
        stage1_threshold=float(entry_policy["selected_threshold"]),
        ce_threshold=float(direction_policy["selected_ce_threshold"]),
        pe_threshold=float(direction_policy["selected_pe_threshold"]),
        min_edge=float(direction_policy["selected_min_edge"]),
        recipe_threshold=0.50,
        recipe_margin_min=0.05,
        recipe_ids=recipe_ids,
    )
    assert combined_summary == expected_combined_summary

    fixed_baseline = staged_pipeline._fixed_recipe_baseline(  # type: ignore[attr-defined]
        utility,
        stage1_scores,
        stage2_scores,
        stage1_threshold=float(entry_policy["selected_threshold"]),
        ce_threshold=float(direction_policy["selected_ce_threshold"]),
        pe_threshold=float(direction_policy["selected_pe_threshold"]),
        min_edge=float(direction_policy["selected_min_edge"]),
        recipe_id="L1",
    )
    expected_fixed_baseline = _legacy_fixed_recipe_baseline(
        utility,
        stage1_scores,
        stage2_scores,
        stage1_threshold=float(entry_policy["selected_threshold"]),
        ce_threshold=float(direction_policy["selected_ce_threshold"]),
        pe_threshold=float(direction_policy["selected_pe_threshold"]),
        min_edge=float(direction_policy["selected_min_edge"]),
        recipe_id="L1",
    )
    assert fixed_baseline == expected_fixed_baseline
