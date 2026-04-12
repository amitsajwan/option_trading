from __future__ import annotations

from snapshot_app.core.stage_views import project_stage_views_v2_from_flat_row


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
