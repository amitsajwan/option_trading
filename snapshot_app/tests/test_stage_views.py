from __future__ import annotations

import pytest

from snapshot_app.core.stage_views import (
    project_stage_views_v2_from_flat_row,
    project_stage1_entry_view_v2_from_flat_row,
    project_stage3_recipe_view_v2_from_flat_row,
)

_REGIME_FLAGS = (
    "ctx_regime_atr_high",
    "ctx_regime_atr_low",
    "ctx_regime_trend_up",
    "ctx_regime_trend_down",
    "ctx_regime_expiry_near",
    "ctx_is_high_vix_day",
)


def test_stage2_direction_view_v2_projects_regime_flags() -> None:
    """WP-1: regime flags present in flat row must appear in stage2 projection."""
    row = {
        "snapshot_id": "snap_regime",
        "instrument": "BANKNIFTY-I",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T12:00:00+05:30",
        "ctx_regime_atr_high": 1.0,
        "ctx_regime_atr_low": 0.0,
        "ctx_regime_trend_up": 1.0,
        "ctx_regime_trend_down": 0.0,
        "ctx_regime_expiry_near": 0.0,
        "ctx_is_high_vix_day": 0.0,
    }
    projected = project_stage_views_v2_from_flat_row(row)
    stage2 = projected["stage2_direction_view_v2"]

    assert stage2["ctx_regime_atr_high"] == 1.0
    assert stage2["ctx_regime_atr_low"] == 0.0
    assert stage2["ctx_regime_trend_up"] == 1.0
    assert stage2["ctx_regime_trend_down"] == 0.0
    assert stage2["ctx_regime_expiry_near"] == 0.0
    assert stage2["ctx_is_high_vix_day"] == 0.0


def test_stage2_direction_view_v2_regime_flags_null_safe() -> None:
    """WP-1: flat row missing regime flag keys must project to None, not raise KeyError."""
    row = {
        "snapshot_id": "snap_null",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T12:00:00+05:30",
        # regime flags deliberately absent
    }
    projected = project_stage_views_v2_from_flat_row(row)
    stage2 = projected["stage2_direction_view_v2"]

    for flag in _REGIME_FLAGS:
        assert flag in stage2, f"'{flag}' key missing from stage2 projection"
        assert stage2[flag] is None, f"'{flag}' expected None when absent from row, got {stage2[flag]!r}"


def test_stage1_and_stage3_unaffected_by_regime_block() -> None:
    """WP-1: regime_context block is stage2-only; stage1 and stage3 must not include it."""
    row = {
        "snapshot_id": "snap_check",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T12:00:00+05:30",
        "ctx_regime_atr_high": 1.0,
        "ctx_regime_trend_up": 1.0,
        "ctx_is_high_vix_day": 1.0,
    }
    stage1 = project_stage1_entry_view_v2_from_flat_row(row)
    stage3 = project_stage3_recipe_view_v2_from_flat_row(row)

    for flag in _REGIME_FLAGS:
        assert flag not in stage1, f"'{flag}' must NOT be in stage1 (regime block is stage2-only)"
        assert flag not in stage3, f"'{flag}' must NOT be in stage3 (regime block is stage2-only)"


def test_project_stage_views_v2_from_flat_row_includes_velocity_and_readiness_fields() -> None:
    row = {
        "snapshot_id": "snap_1",
        "instrument": "BANKNIFTY-I",
        "trade_date": "2024-08-01",
        "timestamp": "2024-08-01T11:30:00+05:30",
        "schema_name": "SnapshotMLFlatV2",
        "schema_version": "4.0",
        "minutes_since_open": 135,
        "day_of_week": 4,
        "vel_ce_oi_delta_open": 12.0,
        "ctx_am_trend": 1.0,
        "adx_14": 24.5,
        "vol_spike_ratio": 2.1,
        "ctx_gap_pct": 0.004,
        "ctx_gap_up": 1,
        "ctx_gap_down": 0,
    }

    projected = project_stage_views_v2_from_flat_row(row)

    assert set(projected) == {
        "stage1_entry_view_v2",
        "stage2_direction_view_v2",
        "stage3_recipe_view_v2",
    }
    stage2 = projected["stage2_direction_view_v2"]
    assert stage2["snapshot_id"] == "snap_1"
    assert stage2["vel_ce_oi_delta_open"] == 12.0
    assert stage2["ctx_am_trend"] == 1.0
    assert stage2["adx_14"] == 24.5
    assert stage2["vol_spike_ratio"] == 2.1
    assert stage2["ctx_gap_pct"] == 0.004
