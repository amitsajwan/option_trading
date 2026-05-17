"""Tests for the JSONL-first current-state reader (ARCHITECTURE.md §9 / Stage 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_data_dashboard.strategy_current_state import (
    _compute_stats,
    _list_available_models,
    _read_runtime_config,
    _tail_lines,
    read_blocker_funnel,
    read_decision_timeline,
    read_strategy_current_state,
)


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_read_returns_empty_when_file_missing(tmp_path: Path):
    out = read_strategy_current_state(mode="replay", run_dir=tmp_path)
    assert out["file_exists"] is False
    assert out["file_size_bytes"] == 0
    assert out["stats"]["total_records"] == 0
    assert out["latest_positions"] == []
    assert out["health_marker"] == {"ok": True}


def test_read_extracts_stats_and_latest(tmp_path: Path):
    records = [
        {"event": "POSITION_OPEN",  "run_id": "r1", "timestamp": "2024-01-01T09:30:00+05:30", "position_id": "p1"},
        {"event": "POSITION_MANAGE", "run_id": "r1", "timestamp": "2024-01-01T09:31:00+05:30", "position_id": "p1"},
        {"event": "POSITION_CLOSE",  "run_id": "r1", "timestamp": "2024-01-01T09:40:00+05:30", "position_id": "p1"},
        {"event": "POSITION_OPEN",  "run_id": "r2", "timestamp": "2024-01-02T10:00:00+05:30", "position_id": "p2"},
    ]
    _write_records(tmp_path / "positions.jsonl", records)
    out = read_strategy_current_state(mode="replay", run_dir=tmp_path, latest_n=10)
    assert out["file_exists"] is True
    assert out["stats"]["total_records"] == 4
    assert out["stats"]["current_run_id"] == "r2"
    assert out["stats"]["run_ids_seen"] == ["r1", "r2"]
    assert out["stats"]["event_counts"] == {
        "POSITION_OPEN": 2, "POSITION_MANAGE": 1, "POSITION_CLOSE": 1,
    }
    assert len(out["latest_positions"]) == 4


def test_latest_n_limits_recent_only(tmp_path: Path):
    records = [{"event": "POSITION_MANAGE", "i": i, "timestamp": f"2024-01-01T{9 + i//60:02d}:{i%60:02d}:00+05:30"} for i in range(120)]
    _write_records(tmp_path / "positions.jsonl", records)
    out = read_strategy_current_state(mode="replay", run_dir=tmp_path, latest_n=5)
    assert len(out["latest_positions"]) == 5
    # latest 5 should be records 115..119
    assert [r["i"] for r in out["latest_positions"]] == [115, 116, 117, 118, 119]


def test_health_marker_red(tmp_path: Path):
    _write_records(tmp_path / "positions.jsonl", [])
    (tmp_path / "health_marker.json").write_text(json.dumps({
        "ok": False, "reason": "jsonl_append_failed", "event_type": "POSITION_OPEN"
    }))
    out = read_strategy_current_state(mode="replay", run_dir=tmp_path)
    assert out["health_marker"]["ok"] is False
    assert out["health_marker"]["event_type"] == "POSITION_OPEN"


def test_health_marker_malformed_is_unhealthy(tmp_path: Path):
    """Defense in depth — same fail-safe behavior as HealthMarker.is_healthy()."""
    _write_records(tmp_path / "positions.jsonl", [])
    (tmp_path / "health_marker.json").write_text("not json {")
    out = read_strategy_current_state(mode="replay", run_dir=tmp_path)
    assert out["health_marker"]["ok"] is False


def test_tail_lines_handles_short_file(tmp_path: Path):
    p = tmp_path / "tiny.jsonl"
    p.write_text("a\nb\nc\n")
    assert _tail_lines(p, n=10) == ["a", "b", "c"]
    assert _tail_lines(p, n=2) == ["b", "c"]


def test_tail_lines_drops_partial_first_line(tmp_path: Path):
    """If we seek into the middle of a file, the first partial line must be discarded."""
    p = tmp_path / "big.jsonl"
    # Build a file larger than max_bytes_back so we exercise the partial-line branch
    lines = [f"event {i:08d}".ljust(100) for i in range(40_000)]  # ~4 MB
    p.write_text("\n".join(lines) + "\n")
    result = _tail_lines(p, n=2, max_bytes_back=4096)
    assert len(result) <= 2
    # Every returned line must be a complete event line (full 100 chars before strip)
    for ln in result:
        assert ln.startswith("event ")


def test_compute_stats_empty():
    s = _compute_stats([])
    assert s["total_records"] == 0
    assert s["current_run_id"] is None


def test_runtime_config_missing_returns_empty(tmp_path: Path):
    out = _read_runtime_config(tmp_path)
    assert out == {}


def test_runtime_config_extracts_key_fields(tmp_path: Path):
    cfg = {
        "engine": "ml_pure",
        "topic": "market:snapshot:v1:historical",
        "strategy_profile_id": "ml_pure_staged_v1",
        "model": {
            "run_id": "staged_deep_hpo_c1_base_20260429_040848",
            "model_group": "banknifty_futures/h15_tp_auto",
            "model_package_path": "/app/...joblib",
            "block_expiry": False,
        },
        "rollout": {
            "stage": "capped_live",
            "min_confidence": 0.65,
            "position_size_multiplier": 0.25,
            "halt_consecutive_losses": 3,
            "halt_daily_dd_pct": -0.75,
        },
        "checked_at_ist": "2026-05-16T17:23:19+05:30",
        "noise_field": "should_be_ignored",
    }
    (tmp_path / "runtime_config.json").write_text(json.dumps(cfg))
    out = _read_runtime_config(tmp_path)
    assert out["engine"] == "ml_pure"
    assert out["model_run_id"] == "staged_deep_hpo_c1_base_20260429_040848"
    assert out["model_group"] == "banknifty_futures/h15_tp_auto"
    assert out["rollout_stage"] == "capped_live"
    assert out["min_confidence"] == 0.65
    assert "noise_field" not in out  # confirms we don't leak unfiltered config


def test_runtime_config_malformed_returns_error(tmp_path: Path):
    (tmp_path / "runtime_config.json").write_text("not json {")
    out = _read_runtime_config(tmp_path)
    assert out == {"error": "runtime_config.json unreadable"}


def test_list_available_models_finds_joblibs(tmp_path: Path):
    # Build a fake published_models tree matching the real layout:
    # <root>/<grp_outer>/<grp_inner>/data/training_runs/<RUN_ID>/model/model.joblib
    base = tmp_path / "published"
    for run_id in ("staged_deep_hpo_c1_base_20260429_040848", "01_expiry_s2_midday"):
        d = base / "banknifty_futures" / "h15_tp_auto" / "data" / "training_runs" / run_id / "model"
        d.mkdir(parents=True)
        (d / "model.joblib").write_bytes(b"fake")
    out = _list_available_models(root=base)
    assert len(out) == 2
    run_ids = sorted(e["run_id"] for e in out)
    assert run_ids == ["01_expiry_s2_midday", "staged_deep_hpo_c1_base_20260429_040848"]
    for e in out:
        assert e["model_group"] == "banknifty_futures/h15_tp_auto"
        assert e["model_package_path"].endswith("model.joblib")


def test_list_available_models_missing_root(tmp_path: Path):
    out = _list_available_models(root=tmp_path / "does-not-exist")
    assert out == []


def test_state_includes_runtime_config_and_available_models(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUN_DIR_HISTORICAL", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "positions.jsonl").write_text("")
    (tmp_path / "run" / "runtime_config.json").write_text(json.dumps({
        "engine": "ml_pure",
        "model": {"run_id": "C1"},
        "rollout": {"stage": "capped_live"},
    }))
    # Empty default published_models — that's fine, list will just be empty
    monkeypatch.setattr(
        "market_data_dashboard.strategy_current_state.DEFAULT_PUBLISHED_MODELS_ROOT",
        tmp_path / "no-models",
    )
    out = read_strategy_current_state(mode="replay")
    assert out["runtime_config"]["engine"] == "ml_pure"
    assert out["runtime_config"]["model_run_id"] == "C1"
    assert out["runtime_config"]["rollout_stage"] == "capped_live"
    assert out["available_models"] == []


def _write_traces(path: Path, traces: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")


def test_blocker_funnel_rejects_bad_date(tmp_path: Path):
    out = read_blocker_funnel(mode="replay", date="not-a-date", run_dir=tmp_path)
    assert "error" in out


def test_blocker_funnel_no_file(tmp_path: Path):
    out = read_blocker_funnel(mode="replay", date="2024-10-07", run_dir=tmp_path)
    assert out["total_traces"] == 0
    assert "no decision_traces.jsonl" in out["narrative"]


def test_blocker_funnel_all_blocked_with_clear_winner(tmp_path: Path):
    # 3 prefilter/regime_sideways + 1 stage2/direction_below_threshold, 0 executed.
    traces = [
        {"trade_date_ist": "2024-10-07", "final_outcome": "blocked",
         "primary_blocker_gate": "prefilter",
         "flow_gates": [{"gate_id": "prefilter", "status": "blocked", "reason_code": "regime_sideways"}]},
        {"trade_date_ist": "2024-10-07", "final_outcome": "blocked",
         "primary_blocker_gate": "prefilter",
         "flow_gates": [{"gate_id": "prefilter", "status": "blocked", "reason_code": "regime_sideways"}]},
        {"trade_date_ist": "2024-10-07", "final_outcome": "blocked",
         "primary_blocker_gate": "prefilter",
         "flow_gates": [{"gate_id": "prefilter", "status": "blocked", "reason_code": "regime_sideways"}]},
        {"trade_date_ist": "2024-10-07", "final_outcome": "hold",
         "primary_blocker_gate": "stage2_direction",
         "flow_gates": [
             {"gate_id": "prefilter", "status": "pass"},
             {"gate_id": "stage2_direction", "status": "hold", "reason_code": "direction_below_threshold"}
         ]},
        # A trace for a DIFFERENT date — must be excluded.
        {"trade_date_ist": "2024-10-08", "final_outcome": "blocked",
         "primary_blocker_gate": "prefilter",
         "flow_gates": [{"gate_id": "prefilter", "status": "blocked", "reason_code": "regime_other"}]},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_blocker_funnel(mode="replay", date="2024-10-07", run_dir=tmp_path)
    assert out["total_traces"] == 4
    assert out["outcomes"] == {"blocked": 3, "hold": 1}
    assert out["primary_blocker_gates"][0] == {"gate": "prefilter", "count": 3}
    assert any(r["reason_code"] == "regime_sideways" for r in out["blocking_reasons"])
    assert any(r["reason_code"] == "direction_below_threshold" for r in out["blocking_reasons"])
    assert "0 produced trades" in out["narrative"]


def test_blocker_funnel_with_entry_taken_trades(tmp_path: Path):
    """Real-pipeline outcome strings: entry_taken (not 'executed')."""
    traces = [
        {"trade_date_ist": "2024-04-09", "final_outcome": "entry_taken",
         "primary_blocker_gate": None, "flow_gates": []},
        {"trade_date_ist": "2024-04-09", "final_outcome": "blocked",
         "primary_blocker_gate": "stage1_threshold",
         "flow_gates": [{"gate_id": "stage1_threshold", "status": "blocked", "reason_code": "entry_below_threshold"}]},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_blocker_funnel(mode="replay", date="2024-04-09", run_dir=tmp_path)
    assert out["total_traces"] == 2
    assert out["outcomes"]["entry_taken"] == 1
    assert "1 produced trades" in out["narrative"]


def test_decision_timeline_no_file(tmp_path: Path):
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path)
    assert out["traces_path_exists"] is False
    assert out["total_for_date"] == 0
    assert out["decisions"] == []


def test_decision_timeline_returns_per_minute_rows(tmp_path: Path):
    traces = [
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T09:15:00+05:30",
         "snapshot_id": "20241007_0915", "final_outcome": "blocked",
         "primary_blocker_gate": "prefilter",
         "flow_gates": [{"gate_id": "prefilter", "status": "blocked",
                         "reason_code": "regime_sideways", "message": "regime blocked"}],
         "summary_metrics": {"entry_prob": 0.1, "recipe_prob": 0.05, "recipe_margin": 0.0,
                             "direction_up_prob": 0.4},
         "regime_context": {"regime": "SIDEWAYS"},
         "model_diagnostics": {
             "stage1": {"input_hash": "abc123", "non_null_count": 42, "output_prob": 0.1},
             "stage2": {"input_hash": "def456", "non_null_count": 61, "output_prob": 0.4},
         }},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T09:16:00+05:30",
         "snapshot_id": "20241007_0916", "final_outcome": "entry_taken",
         "primary_blocker_gate": None, "flow_gates": [],
         "summary_metrics": {"entry_prob": 0.82, "recipe_prob": 0.75, "recipe_margin": 0.10},
         "regime_context": {"regime": "TREND_UP"}},
        # Different date — must be excluded.
        {"trade_date_ist": "2024-10-08", "timestamp": "2024-10-08T09:15:00+05:30",
         "snapshot_id": "20241008_0915", "final_outcome": "blocked",
         "flow_gates": []},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path)
    assert out["total_for_date"] == 2
    assert out["returned"] == 2
    assert [d["time"] for d in out["decisions"]] == ["09:15", "09:16"]
    assert out["decisions"][0]["reason_code"] == "regime_sideways"
    assert out["decisions"][0]["regime"] == "SIDEWAYS"
    assert out["decisions"][0]["model_diagnostics"]["stage1"]["input_hash"] == "abc123"
    assert out["decisions"][0]["model_diagnostics"]["stage2"]["output_prob"] == 0.4
    assert out["decisions"][1]["outcome"] == "entry_taken"


def test_decision_timeline_outcome_filter(tmp_path: Path):
    traces = [
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T09:15:00+05:30",
         "final_outcome": "blocked", "flow_gates": []},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T09:16:00+05:30",
         "final_outcome": "entry_taken", "flow_gates": []},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T09:17:00+05:30",
         "final_outcome": "blocked", "flow_gates": []},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path,
                                 outcome="entry_taken")
    assert out["total_for_date"] == 3
    assert out["matched_filter"] == 1
    assert out["returned"] == 1
    assert out["decisions"][0]["outcome"] == "entry_taken"


def test_decision_timeline_collapse_merges_identical_consecutive_rows(tmp_path: Path):
    """3 holds with bit-identical entry_prob followed by a different one should
    collapse into 2 rows. The 1st row gets time_end + run_minutes=3.

    This is the operator's "Stage-1 stuck at one prob for N minutes" view.
    """
    traces = [
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:05:00+05:30",
         "snapshot_id": "20241007_1305", "final_outcome": "hold",
         "primary_blocker_gate": "stage2_direction",
         "flow_gates": [{"gate_id": "stage2_direction", "status": "hold",
                         "reason_code": "direction_below_threshold"}],
         "summary_metrics": {"entry_prob": 0.5660967826843262, "direction_up_prob": 0.49}},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:06:00+05:30",
         "snapshot_id": "20241007_1306", "final_outcome": "hold",
         "primary_blocker_gate": "stage2_direction",
         "flow_gates": [{"gate_id": "stage2_direction", "status": "hold",
                         "reason_code": "direction_below_threshold"}],
         # SAME entry_prob, different direction_up_prob — should still collapse.
         "summary_metrics": {"entry_prob": 0.5660967826843262, "direction_up_prob": 0.52}},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:07:00+05:30",
         "snapshot_id": "20241007_1307", "final_outcome": "hold",
         "primary_blocker_gate": "stage2_direction",
         "flow_gates": [{"gate_id": "stage2_direction", "status": "hold",
                         "reason_code": "direction_below_threshold"}],
         "summary_metrics": {"entry_prob": 0.5660967826843262, "direction_up_prob": 0.50}},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:08:00+05:30",
         "snapshot_id": "20241007_1308", "final_outcome": "hold",
         "primary_blocker_gate": "stage2_direction",
         "flow_gates": [{"gate_id": "stage2_direction", "status": "hold",
                         "reason_code": "direction_below_threshold"}],
         # DIFFERENT entry_prob — starts a new run.
         "summary_metrics": {"entry_prob": 0.5691027045249939, "direction_up_prob": 0.51}},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path,
                                 collapse=True)
    assert out["collapsed"] is True
    assert out["matched_filter"] == 4  # raw count is preserved
    assert out["returned"] == 2  # 3 + 1 collapsed → 2 rows
    first, second = out["decisions"]
    assert first["time"] == "13:05" and first["time_end"] == "13:07"
    assert first["run_minutes"] == 3
    assert second["time"] == "13:08" and second["time_end"] == "13:08"
    assert second["run_minutes"] == 1


def test_decision_timeline_collapse_off_keeps_all_rows(tmp_path: Path):
    """Without collapse=True, behaviour is unchanged — every row included."""
    traces = [
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:05:00+05:30",
         "final_outcome": "hold", "flow_gates": [],
         "summary_metrics": {"entry_prob": 0.5660967826843262}},
        {"trade_date_ist": "2024-10-07", "timestamp": "2024-10-07T13:06:00+05:30",
         "final_outcome": "hold", "flow_gates": [],
         "summary_metrics": {"entry_prob": 0.5660967826843262}},
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path)
    assert out["collapsed"] is False
    assert out["returned"] == 2
    # No collapse-only fields when collapse is off
    assert "run_minutes" not in out["decisions"][0]


def test_decision_timeline_limit_and_offset(tmp_path: Path):
    traces = [
        {"trade_date_ist": "2024-10-07", "timestamp": f"2024-10-07T09:{i:02d}:00+05:30",
         "final_outcome": "blocked", "flow_gates": []}
        for i in range(15, 25)
    ]
    _write_traces(tmp_path / "decision_traces.jsonl", traces)
    out = read_decision_timeline(mode="replay", date="2024-10-07", run_dir=tmp_path,
                                 limit=3, offset=4)
    assert out["matched_filter"] == 10
    assert out["returned"] == 3
    assert [d["time"] for d in out["decisions"]] == ["09:19", "09:20", "09:21"]


def test_mode_alias_replay_and_historical_both_map(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUN_DIR_HISTORICAL", str(tmp_path / "hist"))
    monkeypatch.setenv("STRATEGY_RUN_DIR_LIVE", str(tmp_path / "live"))
    (tmp_path / "hist").mkdir()
    (tmp_path / "live").mkdir()
    _write_records(tmp_path / "hist" / "positions.jsonl", [{"event": "X", "run_id": "hist1"}])
    _write_records(tmp_path / "live" / "positions.jsonl", [{"event": "Y", "run_id": "live1"}])

    historical_out = read_strategy_current_state(mode="historical")
    replay_out = read_strategy_current_state(mode="replay")
    live_out = read_strategy_current_state(mode="live")

    assert historical_out["stats"]["current_run_id"] == "hist1"
    assert replay_out["stats"]["current_run_id"] == "hist1"
    assert live_out["stats"]["current_run_id"] == "live1"
