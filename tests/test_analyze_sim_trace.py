"""Tests for the decision-trace analyzer + entry-label verifier."""
from __future__ import annotations

from ops.gcp.analyze_sim_trace import analyze_traces, render_decision_card, verify_entry_label


def _entry_trace(ts, prob, *, outcome="hold", direction="CE", selected=False, ms=None,
                 grade_evaluated=False, ce_prob=0.58, bull=0.30, bear=0.40):
    return {
        "timestamp": ts,
        "final_outcome": outcome,
        "direction_source": "ml_entry_timing",
        "regime_context": {"regime": "SIDEWAYS", "confidence": 0.62, "reason": "returns_mixed"},
        "direction": {
            "mode": "consensus", "chosen": direction, "source": "ml_entry_timing",
            "ml_ce_prob": ce_prob, "margin": None,
            "evidence": {"bull_score": bull, "bear_score": bear},
            "grade": None, "tier": None, "grade_evaluated": grade_evaluated,
        },
        "candidates": [{
            "strategy_name": "ML_ENTRY", "direction": direction, "selected": selected,
            "metrics": {"entry_prob": prob}, "terminal_status": "selected" if selected else "blocked",
            "terminal_reason_code": None if selected else "below_threshold",
            "ordered_gates": [
                {"gate_id": "regime_classification", "gate_group": "regime", "status": "pass",
                 "metrics": {"regime_confidence": 0.62}},
                {"gate_id": "direction_evidence", "gate_group": "evidence", "status": "pass",
                 "metrics": {"bull_score": bull, "bear_score": bear}},
            ],
        }],
        "flow_gates": [],
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
    # 0.92 fires (>=0.65), 0.55 is a declined bar -> 1 fired of 2.
    assert rep["ml_entry"]["bars_fired"] == 1
    assert rep["ml_entry"]["bars_with_prob"] == 2
    assert rep["ml_entry"]["fire_rate_pct"] == 50.0
    assert rep["direction"]["ce_pe"] == {"CE": 1}
    assert rep["market_structure"]["position_in_range"] == {"near_high": 1}
    assert rep["gates"]["candidate_veto_reason"].get("below_threshold") == 1
    assert rep["trades_summary"]["losses"] == 1


def test_s7_declined_probs_enable_separation():
    # Fired bar (0.92) sees a move; declined bar (0.40, via entry_model) is flat ->
    # the declined bar becomes the not-fired population, so separation is computable.
    fired = _entry_trace("2026-06-04T09:30:00", 0.92, outcome="entry_taken", selected=True)
    fired["entry_model"] = {"entry_prob": 0.92, "threshold": 0.65, "fired": True}
    declined = {"timestamp": "2026-06-04T09:45:00", "final_outcome": "hold",
                "entry_model": {"entry_prob": 0.40, "threshold": 0.65, "fired": False},
                "candidates": []}
    snaps = _series(54000, 7.0, m0=30, n=12) + _series(54100, 0.2, m0=45, n=12)
    rep = analyze_traces([fired, declined], snapshots=snaps, entry_horizon_minutes=10, entry_min_points=50.0)
    me = rep["ml_entry"]
    assert me["bars_fired"] == 1 and me["bars_with_prob"] == 2
    elc = rep["entry_label_check"]
    assert elc["fired"]["n"] == 1 and elc["not_fired"]["n"] == 1   # declined bar now counted
    assert elc["separation"] is not None


def test_analyze_traces_runs_label_check_when_snapshots_given():
    traces = [_entry_trace("2026-06-04T09:30:00", 0.92)]
    snaps = _series(54000, 7.0)
    rep = analyze_traces(traces, snapshots=snaps, entry_horizon_minutes=10, entry_min_points=50.0)
    assert rep["entry_label_check"]["fired"]["precision"] == 1.0


def test_direction_block_surfaced_in_report():
    # 2 taken CE trades, grader NOT evaluated (consensus mode) -> coverage 0.
    traces = [
        _entry_trace("2026-06-04T10:46:00", 0.92, outcome="entry_taken", selected=True, grade_evaluated=False),
        _entry_trace("2026-06-04T14:01:00", 0.93, outcome="entry_taken", selected=True, grade_evaluated=False),
    ]
    rep = analyze_traces(traces)
    d = rep["direction"]
    assert d["mode"] == {"consensus": 2}
    assert d["ml_ce_prob"]["n"] == 2
    assert d["grade_evaluated_count"] == 0
    assert d["grade_coverage"] == 0.0
    assert d["evidence_bull"]["n"] == 2


def test_decision_card_shows_full_cascade_and_grader_bypass():
    t = _entry_trace("2026-06-04T10:46:00", 0.92, outcome="entry_taken", selected=True, grade_evaluated=False)
    card = render_decision_card(t)
    assert "OUTCOME=ENTRY_TAKEN" in card
    assert "ENTRY    : prob=0.920" in card
    assert "DIRECTION:" in card and "ml_ce_prob=0.58" in card
    assert "grader BYPASSED" in card          # the consensus-mode gap is surfaced inline
    assert "regime_classification" in card and "direction_evidence" in card  # gate cascade
    assert "STRUCTURE:" in card
