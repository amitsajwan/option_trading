"""Bridge ml_pipeline R1S rule JSON to live snapshot evaluation."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ml_pipeline_2.scripts.rules_pipeline.condition_evaluator import (
    evaluate_all_and,
    evaluate_any_or,
)
from ml_pipeline_2.scripts.rules_pipeline.rule_schema import Rule, TradeScoreConfig

from ..contracts import Direction
from ..market.snapshot_accessor import SnapshotAccessor

_VIX_HIGH_THRESHOLD = 20.0


def resolve_high_vix_day(snap: SnapshotAccessor) -> float:
    flag = snap.ctx_is_high_vix_day
    if flag is not None:
        return 1.0 if float(flag) >= 0.5 else 0.0
    vix_prev = snap.vix_prev_close
    if vix_prev is not None and vix_prev >= _VIX_HIGH_THRESHOLD:
        return 1.0
    return 0.0

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_S3_RULE_PATH = (
    _REPO_ROOT / "ml_pipeline_2/configs/rules/r1s_top3/r1s_top3_s3_composite.json"
)


@lru_cache(maxsize=4)
def load_rule(path: str) -> Rule:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Rule.from_dict(payload)


def snapshot_feature_row(snap: SnapshotAccessor) -> dict[str, Any]:
    """Build a one-row feature dict aligned with ml_flat rule columns."""
    minute_since_open = snap.minutes_since_open
    if minute_since_open is None and snap.timestamp is not None:
        minute_since_open = max(0, snap.timestamp.hour * 60 + snap.timestamp.minute - 555)
    time_minute_of_day = (
        (555 + int(minute_since_open)) if minute_since_open is not None else None
    )
    ready = snap.ctx_opening_range_ready
    if ready is None:
        ready = 1.0 if snap.or_ready else 0.0
    breakout_down = snap.ctx_opening_range_breakout_down
    if breakout_down is None:
        breakout_down = 1.0 if snap.orl_broken else 0.0
    breakout_up = snap.ctx_opening_range_breakout_up
    if breakout_up is None:
        breakout_up = 1.0 if snap.orh_broken else 0.0
    ret_5m = snap.ctx_ret_5m if snap.ctx_ret_5m is not None else snap.fut_return_5m
    vwap_distance = snap.ctx_vwap_distance if snap.ctx_vwap_distance is not None else snap.price_vs_vwap
    return {
        "trade_date": snap.trade_date,
        "minute": float(minute_since_open or 0),
        "time_minute_of_day": time_minute_of_day,
        "ctx_opening_range_ready": ready,
        "ctx_opening_range_breakout_down": breakout_down,
        "ctx_opening_range_breakout_up": breakout_up,
        "ret_5m": ret_5m,
        "vwap_distance": vwap_distance,
        "ctx_is_expiry_day": 1.0 if snap.is_expiry_day else 0.0,
        "ctx_is_high_vix_day": resolve_high_vix_day(snap),
    }


def row_passes_entry(snap: SnapshotAccessor, rule: Rule) -> bool:
    row = snapshot_feature_row(snap)
    if row.get("time_minute_of_day") is None:
        return False
    df = pd.DataFrame([row])
    disqualified = bool(evaluate_any_or(df, rule.disqualifiers).iloc[0])
    if disqualified:
        return False
    for group in rule.disqualifier_all_of:
        if bool(evaluate_all_and(df, group).iloc[0]):
            return False
    return bool(evaluate_all_and(df, rule.entry_conditions).iloc[0])


def composite_score(snap: SnapshotAccessor, cfg: TradeScoreConfig) -> float:
    row = snapshot_feature_row(snap)
    df = pd.DataFrame([row])
    from ml_pipeline_2.scripts.rules_pipeline.trade_selection import _row_score

    return float(_row_score(df, 0, cfg))


def default_s3_rule() -> Rule:
    return load_rule(str(DEFAULT_S3_RULE_PATH))


def direction_from_rule(rule: Rule) -> Direction:
    """Map rules_pipeline direction to strategy vote direction (long premium only)."""
    text = str(rule.direction or "").strip().upper()
    if text.endswith("_CE") or text == "CE":
        return Direction.CE
    if text.endswith("_PE") or text == "PE":
        return Direction.PE
    raise ValueError(f"unsupported rule direction for long-option runtime: {rule.direction!r}")
