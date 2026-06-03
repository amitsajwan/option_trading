"""Shared per-day replay engine.

This module is the single implementation of "run the strategy engine over one day's
snapshots and collect trades". Both the OPS same-day sim and the multi-day sim call
this; they must NOT duplicate the logic.

Fidelity rules (from docs/strategy_platform/01_ARCHITECTURE.md §5):
  - Caller must set os.environ to the desired config BEFORE calling replay_day().
  - STRATEGY_RUN_DIR must point to an isolated /tmp path (never the live run dir).
  - STRATEGY_REDIS_PUBLISH_ENABLED must be "0".
  - ML library versions must be pinned to match strategy_app (enforced in the dashboard
    image's requirements.txt; the multi-day CLI must use the same venv).
  - Merge profile risk_config; never overwrite it.

Public API
----------
replay_day(snapshots, trade_date, progress_cb=None) -> (trades, exit_stack_name)
    Run one day. Config is read from os.environ at call time.

DayResult
    Typed dict returned per-day by replay_day.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Tuple, TypedDict


# ── Typed result ──────────────────────────────────────────────────────────────

class TradeRecord(TypedDict):
    time_in:        str
    time_out:       str
    direction:      str
    strike:         Optional[int]
    prem_in:        float
    prem_out:       float
    pnl_pct:        float
    mfe_pct:        float
    mae_pct:        float
    lots:           int
    exit:           str
    source:         str             # always "sim"
    strategy_name:  str             # e.g. "ML_ENTRY", "ORB" — shown as label in tape
    entry_reason:   str             # signal.reason — why the entry fired


class ReplayDiag(TypedDict):
    evaluated:   int
    eval_errors: int
    signals:     int
    entries:     int
    exits:       int
    first_error: Optional[str]


class ReplayResult(TypedDict):
    trades:         List[TradeRecord]
    exit_stack_name: str
    diag:           ReplayDiag
    decision_traces: List[dict]   # per-bar v2 gate cascade (empty under v1)


# ── Core replay ───────────────────────────────────────────────────────────────

def replay_day(
    snapshots: List[dict],
    trade_date: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> ReplayResult:
    """Run the strategy engine over one day's snapshots.

    Args:
        snapshots:   Ordered list of snapshot dicts for the trading day.
        trade_date:  ISO date string "YYYY-MM-DD". Used for session start/end.
        progress_cb: Optional callable(current_index, total) called every 20 bars.
                     The caller uses this to update a job-progress counter without
                     coupling to this module's internals.

    Returns:
        ReplayResult with trades, exit_stack_name, and diagnostics.

    The caller is responsible for setting os.environ to the desired config before
    calling this function, and restoring it after. This module reads config only
    from os.environ — never from its own defaults — to ensure what runs here is
    exactly what would run live.
    """
    _ensure_repo_on_path()

    from strategy_app.engines import DeterministicRuleEngine
    from strategy_app.engines.profiles import build_run_metadata
    from strategy_app.contracts import SignalType
    from strategy_app.position.exit_policy import build_default_exit_stack

    profile_id = os.getenv("STRATEGY_PROFILE_ID", "trader_master_ml_entry_consensus_v1") \
        or "trader_master_ml_entry_consensus_v1"
    min_conf_raw = os.getenv("STRATEGY_MIN_CONFIDENCE", "0.50")
    min_conf = float(min_conf_raw) if str(min_conf_raw).strip() else 0.50

    engine = DeterministicRuleEngine(
        min_confidence=min_conf,
        strategy_profile_id=profile_id,
    )
    exit_stack_name = build_default_exit_stack().name

    # Build run metadata with MERGED risk_config (rule: merge, never overwrite).
    # Overwriting wipes profile flags like allow_non_atm_for_ml_entry / atm_strike_only
    # which silently forces every trade to ATM regardless of strike config.
    run_meta = build_run_metadata(profile_id)
    profile_risk = dict(run_meta.get("risk_config", {}) or {})
    profile_risk.update({
        "rollout_stage": "paper",
        "position_size_multiplier": 1.0,
        "halt_consecutive_losses": int(os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "3")),
        "halt_daily_dd_pct": 0.04,
    })
    run_meta["risk_config"] = profile_risk

    run_id = os.getenv("STRATEGY_RUN_ID", f"sim-{trade_date}")
    engine.set_run_context(run_id, run_meta)

    trade_date_obj = date.fromisoformat(trade_date)
    engine.on_session_start(trade_date_obj)

    diag: ReplayDiag = {
        "evaluated": 0, "eval_errors": 0, "signals": 0,
        "entries": 0, "exits": 0, "first_error": None,
    }

    trades: List[TradeRecord] = []
    decision_traces: List[dict] = []
    _last_trace_id: Optional[str] = None
    current_entry: Optional[dict] = None
    total = len(snapshots)

    for i, snap in enumerate(snapshots):
        if i % 20 == 0 and progress_cb is not None:
            progress_cb(i, total)

        try:
            signal = engine.evaluate(snap)
            diag["evaluated"] += 1
        except Exception as exc:
            diag["eval_errors"] += 1
            if diag["first_error"] is None:
                diag["first_error"] = f"{exc} :: {traceback.format_exc()[-400:]}"
            continue

        # Capture the per-bar gate cascade BEFORE the signal-None short-circuit —
        # the no_trade bars are exactly the ones we need to explain. None under v1.
        _tr = getattr(engine, "last_entry_trace", None)
        if _tr is not None and _tr.get("decision_id") != _last_trace_id:
            _last_trace_id = _tr.get("decision_id")
            decision_traces.append(_tr)

        if signal is None:
            continue

        diag["signals"] += 1
        ts = str(snap.get("timestamp", ""))
        hhmm = ts[11:16] if len(ts) > 15 else "?"

        if signal.signal_type == SignalType.ENTRY:
            diag["entries"] += 1
            current_entry = {
                "time_in":       hhmm,
                "direction":     signal.direction,
                "strike":        signal.strike,
                "prem_in":       float(signal.entry_premium or 0),
                "lots":          signal.max_lots,
                "strategy_name": str(getattr(signal, "strategy_name", "") or ""),
                "entry_reason":  str(getattr(signal, "reason", "") or ""),
            }

        elif signal.signal_type == SignalType.EXIT and current_entry is not None:
            diag["exits"] += 1
            closed = engine._tracker._closed_positions
            if closed:
                cp = closed[-1]
                pnl_pct  = float(cp.get("pnl_pct", 0))
                mfe_pct  = float(cp.get("mfe_pct", 0))
                mae_pct  = float(cp.get("mae_pct", 0))
                exit_prem = float(cp.get("exit_premium", current_entry["prem_in"]))
                label = str(cp.get("exit_policy_triggered") or cp.get("exit_reason") or "")
            else:
                pnl_pct = mfe_pct = mae_pct = 0.0
                exit_prem = current_entry["prem_in"]
                label = signal.exit_reason.value if signal.exit_reason else "?"

            trades.append(TradeRecord(
                time_in=current_entry["time_in"],
                time_out=hhmm,
                direction=current_entry["direction"],
                strike=current_entry["strike"],
                prem_in=current_entry["prem_in"],
                prem_out=exit_prem,
                pnl_pct=pnl_pct,
                mfe_pct=mfe_pct,
                mae_pct=mae_pct,
                lots=current_entry["lots"],
                exit=label,
                source="sim",
                strategy_name=current_entry.get("strategy_name", ""),
                entry_reason=current_entry.get("entry_reason", ""),
            ))
            current_entry = None

    engine.on_session_end(trade_date_obj)

    if progress_cb is not None:
        progress_cb(total, total)

    return ReplayResult(
        trades=trades,
        exit_stack_name=exit_stack_name,
        diag=diag,
        decision_traces=decision_traces,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_repo_on_path() -> None:
    repo = Path("/app")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
