"""Playbook policy: map rules_pipeline exit JSON to live position management.

Hard rules first (underlying + premium stop), then intelligence (trail, thesis
signal), then time — same order as ``execution_sim``. Entry ranking stays in
rule-backed strategies; no discretionary overrides.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ml_pipeline_2.scripts.rules_pipeline.condition_evaluator import evaluate_condition
from ml_pipeline_2.scripts.rules_pipeline.rule_schema import Condition, ExitConfig, Rule

from ..contracts import ExitReason, PositionContext
from ..engines.r1s_rule_runtime import snapshot_feature_row
from ..market.snapshot_accessor import SnapshotAccessor

PLAYBOOK_EXIT_KEY = "playbook_exit"


def is_short_rule(rule: Rule) -> bool:
    return str(rule.direction or "").strip().upper().startswith("SELL_")


def exit_config_to_metrics(exit_cfg: ExitConfig) -> dict[str, Any]:
    return {
        "stop_pct": float(exit_cfg.stop_pct),
        "target_pct": float(exit_cfg.target_pct),
        "time_stop_minutes": int(exit_cfg.time_stop_minutes),
        "eod_force_close_minute": int(exit_cfg.eod_force_close_minute),
        "underlying_stop_pct": exit_cfg.underlying_stop_pct,
        "trail_activation_pct": exit_cfg.trail_activation_pct,
        "trail_giveback_pct": exit_cfg.trail_giveback_pct,
        "signal_exits": [
            {"column": cond.column, "operator": cond.operator, "value": cond.value}
            for cond in exit_cfg.signal_exits
        ],
    }


def metrics_to_exit_config(metrics: dict[str, Any]) -> ExitConfig:
    signal_exits = tuple(
        Condition(**raw) for raw in metrics.get("signal_exits", []) if isinstance(raw, dict)
    )
    return ExitConfig(
        stop_pct=float(metrics.get("stop_pct", 100)),
        target_pct=float(metrics.get("target_pct", 99)),
        time_stop_minutes=int(metrics.get("time_stop_minutes", 45)),
        eod_force_close_minute=int(metrics.get("eod_force_close_minute", 920)),
        signal_exits=signal_exits,
        trail_activation_pct=metrics.get("trail_activation_pct"),
        trail_giveback_pct=metrics.get("trail_giveback_pct"),
        underlying_stop_pct=metrics.get("underlying_stop_pct"),
    )


def playbook_exit_metrics(rule: Rule) -> dict[str, Any]:
    return exit_config_to_metrics(rule.exit_mechanical)


def vote_exit_fractions(exit_cfg: ExitConfig) -> tuple[float, float, Optional[float], int]:
    """Return (stop_loss_pct, target_pct, underlying_stop_pct, max_hold_bars)."""
    underlying = (
        float(exit_cfg.underlying_stop_pct)
        if exit_cfg.underlying_stop_pct is not None
        else None
    )
    return (
        float(exit_cfg.stop_pct) / 100.0,
        float(exit_cfg.target_pct) / 100.0,
        underlying,
        max(1, int(exit_cfg.time_stop_minutes)),
    )


def _signed_pnl_pct(position: PositionContext) -> float:
    return float(position.pnl_pct)


def _underlying_adverse(
    position: PositionContext,
    snap: SnapshotAccessor,
    underlying_stop_pct: float,
) -> bool:
    if underlying_stop_pct <= 0:
        return False
    entry_fut = position.entry_futures_price
    curr_fut = snap.fut_close
    if entry_fut is None or curr_fut is None or entry_fut <= 0:
        return False
    move = (float(curr_fut) - float(entry_fut)) / float(entry_fut)
    if str(position.position_side or "LONG").strip().upper() == "SHORT":
        return move > underlying_stop_pct
    if position.direction == "PE":
        return move > underlying_stop_pct
    return move < -underlying_stop_pct


def _signal_exit_triggered(snap: SnapshotAccessor, exit_cfg: ExitConfig) -> Optional[str]:
    if not exit_cfg.signal_exits:
        return None
    row = snapshot_feature_row(snap)
    df = pd.DataFrame([row])
    for cond in exit_cfg.signal_exits:
        if bool(evaluate_condition(df, cond).iloc[0]):
            return f"signal:{cond.column}"
    return None


def _trail_stop_triggered(position: PositionContext, exit_cfg: ExitConfig) -> bool:
    act = exit_cfg.trail_activation_pct
    give = exit_cfg.trail_giveback_pct
    if act is None or give is None:
        return False
    mfe = float(position.mfe_pct)
    pnl = _signed_pnl_pct(position)
    if mfe < float(act) / 100.0:
        return False
    return pnl <= mfe - float(give) / 100.0


def evaluate_playbook_exit(
    position: PositionContext,
    snap: SnapshotAccessor,
) -> Optional[tuple[ExitReason, str]]:
    """Return (exit_reason, trigger_label) when playbook policy fires."""
    raw_metrics = position.playbook_exit_policy
    if not isinstance(raw_metrics, dict):
        raw_metrics = position.decision_metrics.get(PLAYBOOK_EXIT_KEY)
    if not isinstance(raw_metrics, dict):
        return None
    exit_cfg = metrics_to_exit_config(raw_metrics)
    pnl = _signed_pnl_pct(position)

    if exit_cfg.underlying_stop_pct is not None:
        if _underlying_adverse(position, snap, float(exit_cfg.underlying_stop_pct)):
            return ExitReason.STOP_LOSS, "underlying_stop"

    if pnl <= -float(exit_cfg.stop_pct) / 100.0:
        return ExitReason.STOP_LOSS, "stop_loss"

    if pnl >= float(exit_cfg.target_pct) / 100.0:
        return ExitReason.TARGET_HIT, "target"

    if _trail_stop_triggered(position, exit_cfg):
        return ExitReason.TRAILING_STOP, "trail_stop"

    signal_tag = _signal_exit_triggered(snap, exit_cfg)
    if signal_tag is not None:
        return ExitReason.STRATEGY_EXIT, signal_tag

    ts = snap.timestamp
    minute_of_day = (ts.hour * 60 + ts.minute) if ts is not None else 0
    if minute_of_day >= int(exit_cfg.eod_force_close_minute):
        return ExitReason.TIME_STOP, "eod_force_close"

    if position.max_hold_bars is not None and position.bars_held >= int(position.max_hold_bars):
        return ExitReason.TIME_STOP, "time_stop"

    return None
