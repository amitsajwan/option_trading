"""mode=live_nifty end-to-end through the state layer (2026-07-24).

The dashboard's dual-instrument manual-trading panel reads NIFTY's latest
per-minute engine decision via /api/strategy/decisions?mode=live_nifty.
_resolve_run_dir supported live_nifty all along; the router's validation
set was the only thing rejecting it. These tests pin both halves:
mode -> run_dir resolution (env override + default), and the router's
allowed-modes set actually containing the new modes.
"""
from __future__ import annotations

import json
from pathlib import Path

from market_data_dashboard.routes.strategy_current_routes import _ALLOWED_MODES
from market_data_dashboard.state.strategy_current_state import (
    _resolve_run_dir,
    read_decision_timeline,
)


def test_router_allows_live_nifty_modes():
    assert "live_nifty" in _ALLOWED_MODES
    assert "nifty" in _ALLOWED_MODES


def test_resolve_run_dir_live_nifty_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUN_DIR_LIVE_NIFTY", str(tmp_path))
    assert _resolve_run_dir("live_nifty") == tmp_path
    assert _resolve_run_dir("nifty") == tmp_path


def test_resolve_run_dir_live_nifty_default(monkeypatch):
    monkeypatch.delenv("STRATEGY_RUN_DIR_LIVE_NIFTY", raising=False)
    assert str(_resolve_run_dir("live_nifty")).replace("\\", "/").endswith(
        ".run/strategy_app_nifty"
    )


def test_decision_timeline_reads_nifty_run_dir_via_mode(tmp_path: Path, monkeypatch):
    """No explicit run_dir passed — the mode alone must route to the NIFTY
    dir. This is exactly the call path the /api/strategy/decisions route
    uses, so it proves the panel's data source works end-to-end."""
    monkeypatch.setenv("STRATEGY_RUN_DIR_LIVE_NIFTY", str(tmp_path))
    trace = {
        "trade_date_ist": "2026-07-24",
        "timestamp": "2026-07-24T09:27:00+05:30",
        "snapshot_id": "20260724_0927",
        "final_outcome": "blocked",
        "primary_blocker_gate": "risk_gate",
        "flow_gates": [{"gate_id": "risk_gate", "status": "blocked",
                        "reason_code": "tier_gate", "message": "paper tier"}],
        "summary_metrics": {"entry_prob": 0.574},
    }
    (tmp_path / "decision_traces.jsonl").write_text(json.dumps(trace) + "\n")

    out = read_decision_timeline(mode="live_nifty", date="2026-07-24")
    assert out["traces_path_exists"] is True
    assert out["total_for_date"] == 1
    assert out["decisions"][0]["time"] == "09:27"
