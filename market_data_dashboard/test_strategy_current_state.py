"""Tests for the JSONL-first current-state reader (ARCHITECTURE.md §9 / Stage 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_data_dashboard.strategy_current_state import (
    _compute_stats,
    _tail_lines,
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
