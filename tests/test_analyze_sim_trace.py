"""Tests for the decision-trace analyzer + entry-label verifier."""
from __future__ import annotations

from ops.gcp.analyze_sim_trace import analyze_traces, verify_entry_label


def _entry_trace(ts, prob, *, outcome="hold", direction="CE", selected=False, ms=None):
    return {
        "timestamp": ts,
        "final_outcome": outcome,
        "direction_source": "consensus(direction_ml)",
        "candidates": [{
            "strategy_name": "ML_ENTRY", "direction": direction, "selected": selected,
            "metrics": {"entry_prob": prob}, "terminal_status": "selected" if selected else "blocked",
            "terminal_reason_code": None if selected else "below_threshold",
        }],
        "market_structure": ms or {"position_in_range": {"label": "mid"}},
    }


def _series(start_px, slope, *, m0=30, n=25):
    """Per-minute fut bars from 09:m0, close = start + slope*k, +/-3 wick."""
    return [{
        "timestamp": f"2026-06-04T09:{m0 + k:02d}:00",
        "futures_bar": {"fut_close": start_px + slope * k,
                        "fut_high": start_px + slope * k + 3,
                        "fut_low": start_px + slope * k - 3},
    } for k in range(n)]


def test_entry_label_moved_true_on_big_move():
    traces = [_entry_trace("2026-06-04T09:30:00", 0.92)]
    snaps = _series(54000, 7.0)  # ~70pt move over 10 min
    r = verify_entry_label(traces, snaps, horizon_minutes=10, min_points=50.0)
    assert r["fired"]["n"] == 1
    assert r["fired"]["moved"] == 1
    assert r["fired"]["precision"] == 1.0
    assert r["fired_detail"][0]["moved"] is True


def test_entry_label_moved_false_on_flat_tape():
    traces = [_entry_trace("2026-06-04T09:30:00", 0.92)]
    snaps = _series(54000, 1.0)  # ~10pt drift, below 50
    r = verify_entry_label(traces, snaps, horizon_minutes=10, min_points=50.0)
    assert r["fired"]["moved"] == 0
    assert r["fired"]["precision"] == 0.0


def test_entry_label_separation_fired_vs_notfired():
    # Fired bar sees a move; not-fired bar is flat -> positive separation.
    traces = [_entry_trace("2026-06-04T09:30:00", 0.92),
              _entry_trace("2026-06-04T09:45:00", 0.40)]
    snaps = (_series(54000, 7.0, m0=30, n=12) + _series(54100, 0.2, m0=45, n=12))
    r = verify_entry_label(traces, snaps, horizon_minutes=10, min_points=50.0)
    assert r["fired"]["n"] == 1 and r["not_fired"]["n"] == 1
    assert r["separation"] is not None and r["separation"] > 0


def test_analyze_traces_full_report_shape():
    traces = [
        _entry_trace("2026-06-04T10:46:00", 0.92, outcome="entry_taken", selected=True,
                     ms={"position_in_range": {"label": "near_high", "range_position": 0.91},
                         "breakout_state": {"label": "fakeout_up"},
                         "swing_pivots": {"structure": "range"},
                         "momentum_alignment": {"label": "mixed"}}),
        _entry_trace("2026-06-04T10:47:00", 0.55),
    ]
    rep = analyze_traces(traces, trades=[{"pnl_pct": -0.0036}])
    assert rep["ml_entry"]["cleared_threshold"]["count"] == 1
    assert rep["direction"]["ce_pe"] == {"CE": 1}
    assert rep["market_structure"]["position_in_range"] == {"near_high": 1}
    assert rep["gates"]["candidate_veto_reason"].get("below_threshold") == 1
    assert rep["trades_summary"]["losses"] == 1


def test_analyze_traces_runs_label_check_when_snapshots_given():
    traces = [_entry_trace("2026-06-04T09:30:00", 0.92)]
    snaps = _series(54000, 7.0)
    rep = analyze_traces(traces, snapshots=snaps, entry_horizon_minutes=10, entry_min_points=50.0)
    assert rep["entry_label_check"]["fired"]["precision"] == 1.0
