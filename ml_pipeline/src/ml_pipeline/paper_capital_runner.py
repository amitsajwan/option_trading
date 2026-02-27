import argparse
import json
import os
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import redis

from .live_inference_adapter import (
    DecisionThresholds,
    RedisEventFeatureClient,
    _append_jsonl,
    _build_session_end_event,
    _emit_exit_aware_event,
    _safe_float,
    load_model_package,
    load_thresholds,
    predict_decision_from_row,
)

# Compatibility for model packages persisted when classes were resolved under
# ml_pipeline.paper_capital_runner during training-time execution contexts.
try:
    from .training_cycle import ConstantProbModel as _ConstantProbModel, QuantileClipper as _QuantileClipper

    globals()["QuantileClipper"] = _QuantileClipper
    globals()["ConstantProbModel"] = _ConstantProbModel
except Exception:
    # Keep runtime resilient if training-only dependencies are unavailable.
    pass


@dataclass
class BookState:
    ce_capital: float
    pe_capital: float
    open_position: Optional[Dict[str, object]] = None
    trades_closed: int = 0

    def mark_to_market(self, ce_price: float, pe_price: float) -> Dict[str, float]:
        ce_mtm = float(self.ce_capital)
        pe_mtm = float(self.pe_capital)
        pos = self.open_position
        if isinstance(pos, dict):
            side = str(pos.get("side", ""))
            qty = float(pos.get("qty", 0.0))
            if side == "CE" and np.isfinite(ce_price) and ce_price > 0:
                ce_mtm = qty * float(ce_price)
            elif side == "PE" and np.isfinite(pe_price) and pe_price > 0:
                pe_mtm = qty * float(pe_price)
        return {
            "ce_capital_mtm": float(ce_mtm),
            "pe_capital_mtm": float(pe_mtm),
            "total_capital_mtm": float(ce_mtm + pe_mtm),
        }


@dataclass(frozen=True)
class RiskConfig:
    stop_loss_pct: float = 0.0
    trailing_enabled: bool = False
    trailing_activation_pct: float = 0.10
    trailing_offset_pct: float = 0.05
    trailing_lock_breakeven: bool = True
    stagnation_enabled: bool = False
    stagnation_window_minutes: int = 10
    stagnation_threshold_pct: float = 0.008
    stagnation_volatility_multiplier: float = 2.0
    stagnation_min_hold_minutes: int = 0
    model_exit_policy: str = "strict"
    stop_execution_mode: str = "stop_market"
    stop_limit_offset_pct: float = 0.002
    stop_limit_max_wait_events: int = 3


@dataclass(frozen=True)
class RuntimeGuardConfig:
    max_consecutive_losses: Optional[int] = None
    max_drawdown_pct: float = 0.0


@dataclass(frozen=True)
class QualityPolicyConfig:
    max_entries_per_day: Optional[int] = None
    entry_cutoff_hour: Optional[int] = None
    entry_cooldown_minutes: int = 0
    min_side_prob: float = 0.0
    min_prob_edge: float = 0.0
    skip_weekdays: tuple = ()


_WEEKDAY_TOKEN_TO_INT = {
    "0": 0,
    "mon": 0,
    "monday": 0,
    "1": 1,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "2": 2,
    "wed": 2,
    "wednesday": 2,
    "3": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "4": 4,
    "fri": 4,
    "friday": 4,
    "5": 5,
    "sat": 5,
    "saturday": 5,
    "6": 6,
    "sun": 6,
    "sunday": 6,
}

_MONTH_TOKEN_TO_MM = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def _parse_weekday_tokens(raw: str) -> List[int]:
    if not str(raw or "").strip():
        return []
    parsed: List[int] = []
    for token in str(raw).split(","):
        key = str(token).strip().lower()
        if not key:
            continue
        if key not in _WEEKDAY_TOKEN_TO_INT:
            raise ValueError(f"invalid weekday token: {token!r}")
        parsed.append(int(_WEEKDAY_TOKEN_TO_INT[key]))
    # De-duplicate while preserving order.
    return list(dict.fromkeys(parsed))


def _coerce_weekday_values(values: Optional[Iterable[object]]) -> List[int]:
    if values is None:
        return []
    out: List[int] = []
    for value in values:
        try:
            wd = int(value)
        except Exception:
            continue
        if 0 <= wd <= 6:
            out.append(int(wd))
    return list(dict.fromkeys(out))


def _print_capital_line(ts: str, event_type: str, event_reason: str, side: str, mtm: Dict[str, float]) -> None:
    print(
        f"[{ts}] {event_type:<6} {event_reason:<16} side={side:<2} "
        f"CE={mtm['ce_capital_mtm']:.2f} PE={mtm['pe_capital_mtm']:.2f} TOTAL={mtm['total_capital_mtm']:.2f}"
    )


def _entry_price_for_side(side: str, ce_price: float, pe_price: float) -> float:
    if side == "CE":
        return ce_price
    if side == "PE":
        return pe_price
    return float("nan")


def _decode_redis_payload(data: object) -> Optional[Dict[str, object]]:
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except Exception:
            return None
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _message_timestamp_iso(msg: Dict[str, object], payload: Optional[Dict[str, object]]) -> str:
    if isinstance(payload, dict):
        for key in ("event_time", "timestamp", "generated_at"):
            value = payload.get(key)
            if value:
                return str(value)
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("event_time", "timestamp", "generated_at"):
                value = inner.get(key)
                if value:
                    return str(value)
    return pd.Timestamp.now(tz="Asia/Kolkata").isoformat()


def _extract_options_chain_from_payload(channel: str, payload: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if not channel.startswith("market:options:"):
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return dict(inner)
    return dict(payload)


def _extract_option_ltp_for_side(
    chain: Dict[str, object],
    side: str,
    strike: Optional[int] = None,
) -> Optional[float]:
    side_u = str(side or "").upper().strip()
    if side_u not in {"CE", "PE"}:
        return None
    strike_key = "ce_ltp" if side_u == "CE" else "pe_ltp"
    atm_key = "atm_ce_ltp" if side_u == "CE" else "atm_pe_ltp"
    rows = chain.get("strikes")
    if isinstance(rows, list):
        candidates = [x for x in rows if isinstance(x, dict) and x.get("strike") is not None]
        if candidates:
            chosen = None
            if strike is not None:
                strike_matches: List[Dict[str, object]] = []
                for node in candidates:
                    node_strike = _safe_float(node.get("strike"))
                    if np.isfinite(node_strike) and abs(float(node_strike) - float(strike)) < 1e-9:
                        strike_matches.append(node)
                if strike_matches:
                    chosen = strike_matches[0]
                else:
                    return None
            else:
                atm = _safe_float(chain.get("atm_strike"))
                if np.isfinite(atm):
                    chosen = min(candidates, key=lambda x: abs(float(_safe_float(x.get("strike"))) - float(atm)))
                else:
                    chosen = None
            if isinstance(chosen, dict):
                px = _safe_float(chosen.get(strike_key))
                if np.isfinite(px) and px > 0:
                    return float(px)
    px = _safe_float(chain.get(atm_key))
    if np.isfinite(px) and px > 0:
        return float(px)
    return None


def _to_int_strike(value: object) -> Optional[int]:
    strike = _safe_float(value)
    if not np.isfinite(strike):
        return None
    return int(round(float(strike)))


def _build_option_symbol(instrument: str, expiry_code: str, strike: Optional[int], side: str) -> Optional[str]:
    if side not in {"CE", "PE"}:
        return None
    if not expiry_code or strike is None:
        return None
    raw = str(instrument or "").upper()
    if raw.startswith("BANKNIFTY"):
        base = "BANKNIFTY"
    else:
        base = re.split(r"[-_:]", raw)[0] or raw
    return f"{base}{expiry_code}{int(strike)}{side}"


def _infer_fut_expiry_yyyymm(instrument: str) -> Optional[str]:
    """
    Parse futures symbol like BANKNIFTY26MARFUT -> 202603.
    """
    raw = str(instrument or "").upper().strip()
    match = re.search(r"(\d{2})([A-Z]{3})FUT$", raw)
    if not match:
        return None
    yy = match.group(1)
    mon = _MONTH_TOKEN_TO_MM.get(match.group(2))
    if not mon:
        return None
    return f"20{yy}{mon}"


def _expiry_yyyymm_from_code(expiry_code: str) -> Optional[str]:
    text = str(expiry_code or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return None
    return digits[:6]


def _position_runtime_snapshot(position: Dict[str, object], option_lot_size: float) -> Dict[str, object]:
    qty = float(_safe_float(position.get("qty")))
    lots_equivalent = float("nan")
    if np.isfinite(option_lot_size) and float(option_lot_size) > 0:
        lots_equivalent = qty / float(option_lot_size)
    return {
        "side": str(position.get("side", "")),
        "qty": qty,
        "lots_equivalent": float(lots_equivalent),
        "lot_size": float(option_lot_size),
        "entry_price": float(_safe_float(position.get("entry_price"))),
        "entry_timestamp": str(position.get("entry_timestamp", "")),
        "atm_strike": _to_int_strike(position.get("atm_strike")),
        "expiry_code": str(position.get("expiry_code", "")),
        "option_symbol": position.get("option_symbol"),
        "stop_price": float(_safe_float(position.get("stop_price"))),
        "high_water_price": float(_safe_float(position.get("high_water_price"))),
    }


def _apply_entry(
    book: BookState,
    side: str,
    price: float,
    ts: str,
    fee_fraction: float,
    risk_cfg: RiskConfig,
    atm_strike: Optional[int] = None,
    expiry_code: str = "",
    option_symbol: Optional[str] = None,
) -> bool:
    if not np.isfinite(price) or price <= 0:
        return False
    if side == "CE":
        capital = float(book.ce_capital)
        investable = capital * (1.0 - fee_fraction)
        qty = investable / float(price) if price > 0 else 0.0
    else:
        capital = float(book.pe_capital)
        investable = capital * (1.0 - fee_fraction)
        qty = investable / float(price) if price > 0 else 0.0
    if qty <= 0:
        return False
    stop_price = float("nan")
    if float(risk_cfg.stop_loss_pct) > 0:
        stop_price = float(price) * (1.0 - float(risk_cfg.stop_loss_pct))
    book.open_position = {
        "side": side,
        "qty": float(qty),
        "entry_price": float(price),
        "entry_timestamp": str(ts),
        "atm_strike": int(atm_strike) if atm_strike is not None else None,
        "expiry_code": str(expiry_code or ""),
        "option_symbol": option_symbol,
        "stop_price": (float(stop_price) if np.isfinite(stop_price) else None),
        "high_water_price": float(price),
    }
    return True


def _apply_exit(book: BookState, side: str, price: float, fee_fraction: float) -> bool:
    pos = book.open_position
    if not isinstance(pos, dict):
        return False
    if str(pos.get("side")) != str(side):
        return False
    if not np.isfinite(price) or price <= 0:
        return False
    qty = float(pos.get("qty", 0.0))
    gross_value = qty * float(price)
    net_value = gross_value * (1.0 - fee_fraction)
    if side == "CE":
        book.ce_capital = float(net_value)
    else:
        book.pe_capital = float(net_value)
    book.open_position = None
    book.trades_closed += 1
    return True


def _build_risk_snapshot(book: BookState) -> Optional[Dict[str, float]]:
    pos = book.open_position
    if not isinstance(pos, dict):
        return None
    return {
        "entry_price": float(_safe_float(pos.get("entry_price"))),
        "stop_price": float(_safe_float(pos.get("stop_price"))),
        "high_water_price": float(_safe_float(pos.get("high_water_price"))),
    }


def _update_risk_and_check_stop(book: BookState, current_price: float, risk_cfg: RiskConfig) -> Optional[Dict[str, float]]:
    pos = book.open_position
    if not isinstance(pos, dict):
        return None
    if not np.isfinite(current_price) or current_price <= 0:
        return None

    entry_price = float(_safe_float(pos.get("entry_price")))
    if not np.isfinite(entry_price) or entry_price <= 0:
        return None

    high_water = float(_safe_float(pos.get("high_water_price")))
    if not np.isfinite(high_water) or high_water <= 0:
        high_water = entry_price
    if current_price > high_water:
        high_water = float(current_price)
        pos["high_water_price"] = float(high_water)

    stop_price = float(_safe_float(pos.get("stop_price")))

    if bool(risk_cfg.trailing_enabled) and float(risk_cfg.trailing_offset_pct) > 0:
        activation_price = entry_price * (1.0 + max(0.0, float(risk_cfg.trailing_activation_pct)))
        if high_water >= activation_price:
            candidate = high_water * (1.0 - max(0.0, float(risk_cfg.trailing_offset_pct)))
            if bool(risk_cfg.trailing_lock_breakeven):
                candidate = max(candidate, entry_price)
            if not np.isfinite(stop_price) or candidate > stop_price:
                stop_price = float(candidate)
                pos["stop_price"] = float(stop_price)

    if np.isfinite(stop_price) and float(current_price) <= float(stop_price):
        reason = "trailing_stop" if bool(risk_cfg.trailing_enabled) and float(stop_price) >= float(entry_price) else "stop_loss"
        mode = str(risk_cfg.stop_execution_mode or "stop_market").strip().lower()
        if mode == "stop_limit":
            offset = max(0.0, float(risk_cfg.stop_limit_offset_pct))
            limit_price = float(stop_price) * (1.0 - float(offset))
            if float(current_price) >= float(limit_price):
                return {
                    "reason": f"{reason}_limit_fill",
                    "entry_price": float(entry_price),
                    "stop_price": float(stop_price),
                    "limit_price": float(limit_price),
                    "high_water_price": float(high_water),
                    "current_price": float(current_price),
                    "exit_price": float(current_price),
                    "pending": False,
                }
            pending = pos.get("pending_stop_limit")
            if not isinstance(pending, dict):
                pos["pending_stop_limit"] = {
                    "limit_price": float(limit_price),
                    "events_waited": 0,
                }
                return {
                    "reason": f"{reason}_limit_pending",
                    "entry_price": float(entry_price),
                    "stop_price": float(stop_price),
                    "limit_price": float(limit_price),
                    "high_water_price": float(high_water),
                    "current_price": float(current_price),
                    "pending": True,
                    "state_changed": True,
                }
            waited_raw = _safe_float(pending.get("events_waited"))
            waited = int(waited_raw) if np.isfinite(waited_raw) and waited_raw >= 0 else 0
            waited += 1
            pending["events_waited"] = int(waited)
            if waited >= int(max(1, risk_cfg.stop_limit_max_wait_events)):
                pos["pending_stop_limit"] = None
                return {
                    "reason": f"{reason}_limit_timeout_market",
                    "entry_price": float(entry_price),
                    "stop_price": float(stop_price),
                    "limit_price": float(limit_price),
                    "high_water_price": float(high_water),
                    "current_price": float(current_price),
                    "exit_price": float(current_price),
                    "pending": False,
                }
            return {
                "reason": f"{reason}_limit_pending",
                "entry_price": float(entry_price),
                "stop_price": float(stop_price),
                "limit_price": float(limit_price),
                "high_water_price": float(high_water),
                "current_price": float(current_price),
                "pending": True,
                "state_changed": False,
            }

        return {
            "reason": str(reason),
            "entry_price": float(entry_price),
            "stop_price": float(stop_price),
            "high_water_price": float(high_water),
            "current_price": float(current_price),
            "exit_price": float(current_price),
            "pending": False,
        }

    pending = pos.get("pending_stop_limit")
    if isinstance(pending, dict):
        limit_price = float(_safe_float(pending.get("limit_price")))
        waited_raw = _safe_float(pending.get("events_waited"))
        waited = int(waited_raw) if np.isfinite(waited_raw) and waited_raw >= 0 else 0
        waited += 1
        pending["events_waited"] = int(waited)
        if np.isfinite(limit_price) and float(current_price) >= float(limit_price):
            pos["pending_stop_limit"] = None
            return {
                "reason": "stop_limit_fill",
                "entry_price": float(entry_price),
                "stop_price": float(stop_price),
                "limit_price": float(limit_price),
                "high_water_price": float(high_water),
                "current_price": float(current_price),
                "exit_price": float(current_price),
                "pending": False,
            }
        if waited >= int(max(1, risk_cfg.stop_limit_max_wait_events)):
            pos["pending_stop_limit"] = None
            return {
                "reason": "stop_limit_timeout_market",
                "entry_price": float(entry_price),
                "stop_price": float(stop_price),
                "limit_price": float(limit_price),
                "high_water_price": float(high_water),
                "current_price": float(current_price),
                "exit_price": float(current_price),
                "pending": False,
            }
        return {
            "reason": "stop_limit_pending",
            "entry_price": float(entry_price),
            "stop_price": float(stop_price),
            "limit_price": float(limit_price),
            "high_water_price": float(high_water),
            "current_price": float(current_price),
            "pending": True,
            "state_changed": False,
        }
    return None


def run_live_redis_capital_loop(
    *,
    instrument: str,
    model_package: Dict[str, object],
    thresholds: DecisionThresholds,
    initial_ce_capital: float,
    initial_pe_capital: float,
    output_jsonl: Optional[Path],
    feature_trace_jsonl: Optional[Path] = None,
    mode: str = "dual",
    redis_host: str = "localhost",
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: Optional[str] = None,
    redis_timeout_seconds: float = 2.0,
    ohlc_pattern: Optional[str] = None,
    options_channel: Optional[str] = None,
    depth_channel: Optional[str] = None,
    max_iterations: Optional[int] = None,
    max_hold_minutes: int = 5,
    confidence_buffer: float = 0.05,
    fee_bps: float = 0.0,
    max_idle_seconds: float = 120.0,
    stop_loss_pct: float = 0.0,
    trailing_enabled: bool = False,
    trailing_activation_pct: float = 0.10,
    trailing_offset_pct: float = 0.05,
    trailing_lock_breakeven: bool = True,
    stagnation_enabled: bool = False,
    stagnation_window_minutes: int = 10,
    stagnation_threshold_pct: float = 0.008,
    stagnation_volatility_multiplier: float = 2.0,
    stagnation_min_hold_minutes: int = 0,
    model_exit_policy: str = "strict",
    stop_execution_mode: str = "stop_market",
    stop_limit_offset_pct: float = 0.002,
    stop_limit_max_wait_events: int = 3,
    option_lot_size: float = 15.0,
    runtime_guard_max_consecutive_losses: Optional[int] = None,
    runtime_guard_max_drawdown_pct: float = 0.0,
    quality_max_entries_per_day: Optional[int] = None,
    quality_entry_cutoff_hour: Optional[int] = None,
    quality_entry_cooldown_minutes: int = 0,
    quality_min_side_prob: float = 0.0,
    quality_min_prob_edge: float = 0.0,
    quality_skip_weekdays: Optional[Iterable[object]] = None,
) -> Dict[str, object]:
    fee_fraction = float(max(0.0, fee_bps) / 10000.0)
    model_exit_policy = str(model_exit_policy or "strict").strip().lower()
    if model_exit_policy not in {"strict", "signal_only", "stop_only", "training_parity"}:
        raise ValueError("model_exit_policy must be one of: strict, signal_only, stop_only, training_parity")
    stop_execution_mode = str(stop_execution_mode or "stop_market").strip().lower()
    if stop_execution_mode not in {"stop_market", "stop_limit"}:
        raise ValueError("stop_execution_mode must be one of: stop_market, stop_limit")
    risk_cfg = RiskConfig(
        stop_loss_pct=max(0.0, float(stop_loss_pct)),
        trailing_enabled=bool(trailing_enabled),
        trailing_activation_pct=max(0.0, float(trailing_activation_pct)),
        trailing_offset_pct=max(0.0, float(trailing_offset_pct)),
        trailing_lock_breakeven=bool(trailing_lock_breakeven),
        stagnation_enabled=bool(stagnation_enabled),
        stagnation_window_minutes=int(max(2, stagnation_window_minutes)),
        stagnation_threshold_pct=max(0.0, float(stagnation_threshold_pct)),
        stagnation_volatility_multiplier=max(0.0, float(stagnation_volatility_multiplier)),
        stagnation_min_hold_minutes=max(0, int(stagnation_min_hold_minutes)),
        model_exit_policy=model_exit_policy,
        stop_execution_mode=stop_execution_mode,
        stop_limit_offset_pct=max(0.0, float(stop_limit_offset_pct)),
        stop_limit_max_wait_events=int(max(1, stop_limit_max_wait_events)),
    )
    guard_cfg = RuntimeGuardConfig(
        max_consecutive_losses=(
            int(runtime_guard_max_consecutive_losses)
            if runtime_guard_max_consecutive_losses is not None and int(runtime_guard_max_consecutive_losses) > 0
            else None
        ),
        max_drawdown_pct=max(0.0, float(runtime_guard_max_drawdown_pct)),
    )
    quality_cfg = QualityPolicyConfig(
        max_entries_per_day=(
            int(quality_max_entries_per_day)
            if quality_max_entries_per_day is not None and int(quality_max_entries_per_day) > 0
            else None
        ),
        entry_cutoff_hour=(
            int(quality_entry_cutoff_hour)
            if quality_entry_cutoff_hour is not None and 0 <= int(quality_entry_cutoff_hour) <= 23
            else None
        ),
        entry_cooldown_minutes=max(0, int(quality_entry_cooldown_minutes)),
        min_side_prob=float(min(1.0, max(0.0, quality_min_side_prob))),
        min_prob_edge=float(min(1.0, max(0.0, quality_min_prob_edge))),
        skip_weekdays=tuple(_coerce_weekday_values(quality_skip_weekdays)),
    )
    book = BookState(ce_capital=float(initial_ce_capital), pe_capital=float(initial_pe_capital))
    initial_total_capital = float(initial_ce_capital + initial_pe_capital)
    realized_total_prev = float(initial_total_capital)
    consecutive_loss_exits = 0
    peak_total_capital_mtm = float(initial_total_capital)
    max_drawdown_observed = 0.0
    runtime_halted = False
    runtime_halt_reason: Optional[str] = None
    runtime_halt_timestamp: Optional[str] = None

    ohlc_subscription = str(ohlc_pattern or f"market:ohlc:{instrument}:*")
    options_subscription = str(options_channel or f"market:options:{instrument}")
    depth_subscription = str(depth_channel or f"market:depth:{instrument}")
    options_alt = f"market:options:{str(instrument).upper()}"
    depth_alt = f"market:depth:{str(instrument).upper()}"

    conn_kwargs: Dict[str, object] = {
        "host": str(redis_host),
        "port": int(redis_port),
        "db": int(redis_db),
        "decode_responses": True,
        "socket_connect_timeout": float(redis_timeout_seconds),
        "socket_timeout": float(redis_timeout_seconds),
    }
    if redis_password:
        conn_kwargs["password"] = str(redis_password)
    client = redis.Redis(**conn_kwargs)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.psubscribe(ohlc_subscription)
    pubsub.subscribe(options_subscription)
    if options_alt != options_subscription:
        pubsub.subscribe(options_alt)
    pubsub.subscribe(depth_subscription)
    if depth_alt != depth_subscription:
        pubsub.subscribe(depth_alt)

    feature_client = RedisEventFeatureClient(
        instrument=instrument,
        max_bars=120,
        redis_client=client,
        mode_hint=str(mode or "").strip().lower(),
    )

    events: List[Dict[str, object]] = []
    position_state: Optional[Dict[str, object]] = None
    last_ts: Optional[str] = None
    last_ce_price = float("nan")
    last_pe_price = float("nan")
    last_atm_strike: Optional[int] = None
    last_expiry_code = ""
    bars_processed = 0
    messages_total = 0
    idle_start = time.monotonic()
    last_decision: Optional[Dict[str, object]] = None
    daily_entries: Dict[str, int] = {}
    last_entry_timestamp: Optional[pd.Timestamp] = None
    entries_taken_total = 0
    quality_skip_weekdays_set = {int(x) for x in quality_cfg.skip_weekdays}
    feature_columns = [str(col) for col in list(model_package.get("feature_columns", []))]
    underlying_expiry_yyyymm = _infer_fut_expiry_yyyymm(instrument)
    current_chain_expiry_code: Optional[str] = None
    current_chain_expiry_yyyymm: Optional[str] = None
    current_expiry_mismatch: bool = False

    def _parse_event_timestamp(ts_value: object) -> Optional[pd.Timestamp]:
        if ts_value is None:
            return None
        try:
            return pd.Timestamp(str(ts_value))
        except Exception:
            return None

    def _entry_block_reason(event_payload: Dict[str, object]) -> Optional[str]:
        trade_day = str(event_payload.get("trade_date") or "").strip()
        ts = _parse_event_timestamp(event_payload.get("timestamp"))
        if not trade_day and ts is not None:
            trade_day = str(ts.date())

        if quality_cfg.max_entries_per_day is not None and trade_day:
            if int(daily_entries.get(trade_day, 0)) >= int(quality_cfg.max_entries_per_day):
                return "quality_block_daily_cap"

        if ts is not None and quality_cfg.entry_cutoff_hour is not None:
            if int(ts.hour) >= int(quality_cfg.entry_cutoff_hour):
                return "quality_block_cutoff_hour"

        if ts is not None and quality_cfg.entry_cooldown_minutes > 0 and last_entry_timestamp is not None:
            delta = (ts - last_entry_timestamp) / pd.Timedelta(minutes=1)
            if np.isfinite(delta) and float(delta) < float(quality_cfg.entry_cooldown_minutes):
                return "quality_block_cooldown"

        if ts is not None and quality_skip_weekdays_set:
            weekday = int(ts.weekday())
            if weekday in quality_skip_weekdays_set:
                return "quality_block_weekday"

        action = str(event_payload.get("action", "")).upper().strip()
        if action not in {"BUY_CE", "BUY_PE"}:
            return None
        side_prob_key = "ce_prob" if action == "BUY_CE" else "pe_prob"
        other_prob_key = "pe_prob" if action == "BUY_CE" else "ce_prob"
        side_prob = _safe_float(event_payload.get(side_prob_key))
        other_prob = _safe_float(event_payload.get(other_prob_key))
        if np.isfinite(side_prob) and float(side_prob) < float(quality_cfg.min_side_prob):
            return "quality_block_min_side_prob"
        if np.isfinite(side_prob) and np.isfinite(other_prob):
            edge = float(side_prob) - float(other_prob)
            if edge < float(quality_cfg.min_prob_edge):
                return "quality_block_min_prob_edge"
        return None

    def _register_exit_pnl() -> float:
        nonlocal consecutive_loss_exits, realized_total_prev
        realized_total_now = float(book.ce_capital + book.pe_capital)
        trade_pnl = float(realized_total_now - realized_total_prev)
        if trade_pnl < 0:
            consecutive_loss_exits += 1
        else:
            consecutive_loss_exits = 0
        realized_total_prev = float(realized_total_now)
        return float(trade_pnl)

    def _evaluate_runtime_halt(event_ts: str, event_type: str) -> Optional[str]:
        nonlocal runtime_halted, runtime_halt_reason, runtime_halt_timestamp
        if runtime_halted:
            return runtime_halt_reason
        reasons: List[str] = []
        if guard_cfg.max_consecutive_losses is not None and int(consecutive_loss_exits) >= int(guard_cfg.max_consecutive_losses):
            reasons.append("consecutive_losses")
        if float(guard_cfg.max_drawdown_pct) > 0 and abs(float(max_drawdown_observed)) >= float(guard_cfg.max_drawdown_pct):
            reasons.append("drawdown")
        if reasons:
            runtime_halted = True
            runtime_halt_reason = "+".join(reasons)
            runtime_halt_timestamp = str(event_ts)
        return runtime_halt_reason

    def _record_event(
        event: Dict[str, object],
        *,
        risk_details: Optional[Dict[str, object]] = None,
        exit_position_snapshot: Optional[Dict[str, object]] = None,
        side_hint: str = "",
        trade_pnl: Optional[float] = None,
    ) -> None:
        nonlocal peak_total_capital_mtm, max_drawdown_observed
        mtm = book.mark_to_market(ce_price=last_ce_price, pe_price=last_pe_price)
        total_cap = float(mtm["total_capital_mtm"])
        peak_total_capital_mtm = max(float(peak_total_capital_mtm), float(total_cap))
        drawdown = 0.0
        if float(peak_total_capital_mtm) > 0:
            drawdown = float(total_cap / float(peak_total_capital_mtm) - 1.0)
        max_drawdown_observed = min(float(max_drawdown_observed), float(drawdown))
        halt_reason = _evaluate_runtime_halt(str(event.get("timestamp", "")), str(event.get("event_type", "")))

        event["capital"] = {
            "ce_capital_mtm": mtm["ce_capital_mtm"],
            "pe_capital_mtm": mtm["pe_capital_mtm"],
            "total_capital_mtm": mtm["total_capital_mtm"],
            "ce_capital_realized": float(book.ce_capital),
            "pe_capital_realized": float(book.pe_capital),
        }
        event["prices"] = {
            "opt_0_ce_close": float(last_ce_price) if np.isfinite(last_ce_price) else float("nan"),
            "opt_0_pe_close": float(last_pe_price) if np.isfinite(last_pe_price) else float("nan"),
        }
        # Persist contract context on every event so replay analysis can bind
        # decisions/exits to the strike/expiry/symbol used at runtime.
        contract_snapshot: Optional[Dict[str, object]] = None
        if isinstance(book.open_position, dict):
            contract_snapshot = dict(book.open_position)
        elif isinstance(exit_position_snapshot, dict):
            contract_snapshot = dict(exit_position_snapshot)

        event_atm_strike = _to_int_strike(event.get("atm_strike"))
        if event_atm_strike is None and isinstance(contract_snapshot, dict):
            event_atm_strike = _to_int_strike(contract_snapshot.get("atm_strike"))
        if event_atm_strike is None and last_atm_strike is not None:
            event_atm_strike = int(last_atm_strike)

        event_expiry_code = str(event.get("expiry_code") or "").upper().strip()
        if not event_expiry_code and isinstance(contract_snapshot, dict):
            event_expiry_code = str(contract_snapshot.get("expiry_code") or "").upper().strip()
        if not event_expiry_code:
            event_expiry_code = str(last_expiry_code or "").upper().strip()

        event_side = str(side_hint or "").upper().strip()
        if event_side not in {"CE", "PE"}:
            action = str(event.get("action") or "").upper().strip()
            if action == "BUY_CE":
                event_side = "CE"
            elif action == "BUY_PE":
                event_side = "PE"
            elif isinstance(contract_snapshot, dict):
                snapshot_side = str(contract_snapshot.get("side") or "").upper().strip()
                if snapshot_side in {"CE", "PE"}:
                    event_side = snapshot_side

        event_option_symbol = str(event.get("option_symbol") or "").upper().strip()
        if not event_option_symbol and isinstance(contract_snapshot, dict):
            event_option_symbol = str(contract_snapshot.get("option_symbol") or "").upper().strip()
        if not event_option_symbol and event_side in {"CE", "PE"}:
            generated_symbol = _build_option_symbol(
                instrument=instrument,
                expiry_code=event_expiry_code,
                strike=event_atm_strike,
                side=event_side,
            )
            if generated_symbol:
                event_option_symbol = str(generated_symbol).upper().strip()

        event["atm_strike"] = int(event_atm_strike) if event_atm_strike is not None else None
        event["expiry_code"] = event_expiry_code or None
        event["option_symbol"] = event_option_symbol or None

        if isinstance(event.get("position"), dict):
            pos_payload = event["position"]
            if _to_int_strike(pos_payload.get("atm_strike")) is None and event["atm_strike"] is not None:
                pos_payload["atm_strike"] = int(event["atm_strike"])
            if not str(pos_payload.get("expiry_code") or "").strip() and event["expiry_code"]:
                pos_payload["expiry_code"] = str(event["expiry_code"])
            if not str(pos_payload.get("option_symbol") or "").strip() and event["option_symbol"]:
                pos_payload["option_symbol"] = str(event["option_symbol"])

        if isinstance(book.open_position, dict):
            event["position_runtime"] = _position_runtime_snapshot(book.open_position, option_lot_size=float(option_lot_size))
        elif isinstance(exit_position_snapshot, dict):
            event["position_runtime"] = _position_runtime_snapshot(exit_position_snapshot, option_lot_size=float(option_lot_size))
        risk_snapshot = _build_risk_snapshot(book)
        if risk_snapshot is not None:
            event["risk"] = risk_snapshot
        elif risk_details is not None:
            event["risk"] = risk_details
        guard_payload = {
            "is_halted": bool(runtime_halted),
            "halt_reason": str(halt_reason) if halt_reason else None,
            "halt_timestamp": str(runtime_halt_timestamp) if runtime_halt_timestamp else None,
            "consecutive_loss_exits": int(consecutive_loss_exits),
            "max_consecutive_losses": int(guard_cfg.max_consecutive_losses) if guard_cfg.max_consecutive_losses is not None else None,
            "drawdown_current": float(drawdown),
            "drawdown_max_observed": float(max_drawdown_observed),
            "max_drawdown_pct": float(guard_cfg.max_drawdown_pct),
        }
        if trade_pnl is not None:
            guard_payload["last_trade_pnl"] = float(trade_pnl)
        event["runtime_guard"] = guard_payload

        events.append(event)
        if output_jsonl is not None:
            _append_jsonl(output_jsonl, [event])
        _print_capital_line(
            ts=str(event.get("timestamp")),
            event_type=str(event.get("event_type", "")),
            event_reason=str(event.get("event_reason", "")),
            side=(side_hint or "--"),
            mtm=mtm,
        )

    def _record_feature_trace(row_payload: Dict[str, object], decision_payload: Dict[str, object]) -> None:
        if feature_trace_jsonl is None:
            return
        feature_map: Dict[str, object] = {col: row_payload.get(col) for col in feature_columns}
        missing_count = 0
        for value in feature_map.values():
            try:
                if value is None or (
                    isinstance(value, (float, np.floating)) and np.isnan(float(value))
                ):
                    missing_count += 1
            except Exception:
                continue
        trace_row: Dict[str, object] = {
            "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
            "timestamp": str(row_payload.get("timestamp", "")),
            "trade_date": str(row_payload.get("trade_date", "")),
            "instrument": instrument,
            "underlying_expiry_yyyymm": underlying_expiry_yyyymm,
            "option_chain_expiry_code": current_chain_expiry_code,
            "option_chain_expiry_yyyymm": current_chain_expiry_yyyymm,
            "expiry_mismatch": bool(current_expiry_mismatch),
            "mode": mode,
            "action": str(decision_payload.get("action", "")),
            "ce_prob": _safe_float(decision_payload.get("ce_prob")),
            "pe_prob": _safe_float(decision_payload.get("pe_prob")),
            "ce_threshold": float(thresholds.ce),
            "pe_threshold": float(thresholds.pe),
            "feature_count": int(len(feature_columns)),
            "feature_missing_count": int(missing_count),
            "features": feature_map,
        }
        _append_jsonl(feature_trace_jsonl, [trace_row])

    try:
        while True:
            msg = pubsub.get_message(timeout=max(0.1, float(redis_timeout_seconds)))
            if msg is None:
                if max_idle_seconds is not None and (time.monotonic() - idle_start) >= float(max_idle_seconds):
                    break
                continue
            idle_start = time.monotonic()
            messages_total += 1
            payload = _decode_redis_payload(msg.get("data"))
            msg_channel = str(msg.get("channel") or "")
            msg_ts = _message_timestamp_iso(msg, payload)
            try:
                trade_date = str(pd.Timestamp(msg_ts).date())
            except Exception:
                trade_date = ""

            chain = _extract_options_chain_from_payload(msg_channel, payload)
            if isinstance(chain, dict):
                side_for_live = str((book.open_position or {}).get("side") or "").upper().strip()
                strike_for_live = _to_int_strike((book.open_position or {}).get("atm_strike"))
                if side_for_live in {"CE", "PE"}:
                    # While in-position, risk checks must use the held strike only.
                    px = _extract_option_ltp_for_side(chain, side_for_live, strike=strike_for_live)
                    if side_for_live == "CE" and px is not None:
                        last_ce_price = float(px)
                    elif side_for_live == "PE" and px is not None:
                        last_pe_price = float(px)
                else:
                    # When flat, keep last observed ATM prices for entry simulation context.
                    atm_ce = _extract_option_ltp_for_side(chain, "CE", strike=None)
                    atm_pe = _extract_option_ltp_for_side(chain, "PE", strike=None)
                    if atm_ce is not None:
                        last_ce_price = float(atm_ce)
                    if atm_pe is not None:
                        last_pe_price = float(atm_pe)

            emitted_ts = feature_client.consume_redis_message(msg)
            new_bar = emitted_ts is not None and emitted_ts != last_ts
            row: Optional[Dict[str, object]] = None
            atm_strike: Optional[int] = None
            expiry_code = ""
            if new_bar:
                try:
                    row = feature_client.build_latest_feature_row()
                except Exception:
                    row = None
                if isinstance(row, dict):
                    ce_px = _safe_float(row.get("opt_0_ce_close"))
                    pe_px = _safe_float(row.get("opt_0_pe_close"))
                    # Avoid replacing held-contract live price with current ATM price while in position.
                    open_side = str((book.open_position or {}).get("side") or "").upper().strip()
                    if np.isfinite(ce_px) and ce_px > 0 and open_side != "CE":
                        last_ce_price = float(ce_px)
                    if np.isfinite(pe_px) and pe_px > 0 and open_side != "PE":
                        last_pe_price = float(pe_px)
                    atm_strike = _to_int_strike(row.get("atm_strike"))
                    expiry_code = str(row.get("expiry_code") or "").upper().strip()
                    current_chain_expiry_code = expiry_code or None
                    current_chain_expiry_yyyymm = _expiry_yyyymm_from_code(expiry_code)
                    current_expiry_mismatch = bool(
                        underlying_expiry_yyyymm
                        and current_chain_expiry_yyyymm
                        and underlying_expiry_yyyymm != current_chain_expiry_yyyymm
                    )
                    if atm_strike is not None:
                        last_atm_strike = int(atm_strike)
                    if expiry_code:
                        last_expiry_code = str(expiry_code)

            # Tick-level risk checks: evaluate on every incoming message.
            if isinstance(book.open_position, dict):
                open_side = str(book.open_position.get("side", "")).upper().strip()
                live_price = _entry_price_for_side(open_side, last_ce_price, last_pe_price)
                stop_details = _update_risk_and_check_stop(book, current_price=live_price, risk_cfg=risk_cfg)
                if isinstance(stop_details, dict):
                    if bool(stop_details.get("pending")):
                        if bool(stop_details.get("state_changed")):
                            pending_event = {
                                "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
                                "timestamp": str(msg_ts),
                                "trade_date": trade_date,
                                "mode": mode,
                                "ce_prob": _safe_float((last_decision or {}).get("ce_prob")),
                                "pe_prob": _safe_float((last_decision or {}).get("pe_prob")),
                                "ce_threshold": float(thresholds.ce),
                                "pe_threshold": float(thresholds.pe),
                                "action": "HOLD",
                                "confidence": _safe_float((last_decision or {}).get("confidence")),
                                "event_type": "MANAGE",
                                "event_reason": str(stop_details.get("reason") or "stop_limit_pending"),
                                "position": (
                                    dict(position_state)
                                    if isinstance(position_state, dict)
                                    else {
                                        "side": open_side,
                                        "entry_timestamp": str(book.open_position.get("entry_timestamp", msg_ts)),
                                    }
                                ),
                                "source": "redis_pubsub",
                                "instrument": instrument,
                            }
                            _record_event(pending_event, risk_details=stop_details, side_hint=open_side)
                    else:
                        pos_snapshot = (
                            dict(position_state)
                            if isinstance(position_state, dict)
                            else {
                                "side": open_side,
                                "entry_timestamp": str(book.open_position.get("entry_timestamp", msg_ts)),
                            }
                        )
                        held_minutes = 0
                        try:
                            held_minutes = int(
                                (
                                    pd.Timestamp(str(msg_ts))
                                    - pd.Timestamp(str(pos_snapshot.get("entry_timestamp")))
                                )
                                / pd.Timedelta(minutes=1)
                            )
                        except Exception:
                            held_minutes = 0
                        exit_snapshot = dict(book.open_position)
                        exit_price = _safe_float(stop_details.get("exit_price"))
                        if not np.isfinite(exit_price) or exit_price <= 0:
                            exit_price = live_price
                        _apply_exit(book, side=open_side, price=exit_price, fee_fraction=fee_fraction)
                        trade_pnl = _register_exit_pnl()
                        position_state = None
                        stop_event = {
                            "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
                            "timestamp": str(msg_ts),
                            "trade_date": trade_date,
                            "mode": mode,
                            "ce_prob": _safe_float((last_decision or {}).get("ce_prob")),
                            "pe_prob": _safe_float((last_decision or {}).get("pe_prob")),
                            "ce_threshold": float(thresholds.ce),
                            "pe_threshold": float(thresholds.pe),
                            "action": "HOLD",
                            "confidence": _safe_float((last_decision or {}).get("confidence")),
                            "event_type": "EXIT",
                            "event_reason": str(stop_details.get("reason") or "stop_loss"),
                            "held_minutes": int(max(0, held_minutes)),
                            "position": pos_snapshot,
                            "source": "redis_pubsub",
                            "instrument": instrument,
                        }
                        _record_event(
                            stop_event,
                            risk_details=stop_details,
                            exit_position_snapshot=exit_snapshot,
                            side_hint=open_side,
                            trade_pnl=trade_pnl,
                        )
                        if new_bar:
                            last_ts = emitted_ts
                            bars_processed += 1
                            if max_iterations is not None and bars_processed >= int(max_iterations):
                                break
                        continue

            if not new_bar or not isinstance(row, dict):
                continue

            decision = predict_decision_from_row(
                row,
                model_package,
                thresholds,
                mode=mode,
                require_complete_row_inputs=True,
            )
            decision["source"] = "redis_pubsub"
            decision["instrument"] = instrument
            decision["underlying_expiry_yyyymm"] = underlying_expiry_yyyymm
            decision["option_chain_expiry_code"] = current_chain_expiry_code
            decision["option_chain_expiry_yyyymm"] = current_chain_expiry_yyyymm
            decision["expiry_mismatch"] = bool(current_expiry_mismatch)
            _record_feature_trace(row, decision)
            last_decision = dict(decision)
            event, position_state = _emit_exit_aware_event(
                decision=decision,
                position=position_state,
                thresholds=thresholds,
                max_hold_minutes=int(max_hold_minutes),
                confidence_buffer=float(confidence_buffer),
                current_ce_price=float(last_ce_price),
                current_pe_price=float(last_pe_price),
                stagnation_enabled=bool(risk_cfg.stagnation_enabled),
                stagnation_window_minutes=int(risk_cfg.stagnation_window_minutes),
                stagnation_threshold_pct=float(risk_cfg.stagnation_threshold_pct),
                stagnation_volatility_multiplier=float(risk_cfg.stagnation_volatility_multiplier),
                stagnation_min_hold_minutes=int(risk_cfg.stagnation_min_hold_minutes),
            )

            side = ""
            if isinstance(event.get("position"), dict):
                side = str(event["position"].get("side", ""))
            event_type = str(event.get("event_type", ""))
            event_reason = str(event.get("event_reason", ""))
            exit_position_snapshot: Optional[Dict[str, object]] = None

            # Optional policy controls for model-generated exits:
            # - signal_only: ignore time/confidence fades; keep signal-flip exits.
            # - stop_only: ignore all model exits; only risk stops should flatten.
            # - training_parity: suppress non-label exits (signal_flip/confidence_fade),
            #   keep time_stop aligned with training path exits.
            if event_type == "EXIT":
                suppress_reasons: set[str] = set()
                if risk_cfg.model_exit_policy == "signal_only":
                    suppress_reasons = {"time_stop", "confidence_fade", "stagnation"}
                elif risk_cfg.model_exit_policy == "stop_only":
                    suppress_reasons = {"time_stop", "confidence_fade", "signal_flip", "stagnation"}
                elif risk_cfg.model_exit_policy == "training_parity":
                    suppress_reasons = {"confidence_fade", "signal_flip", "stagnation"}
                if event_reason in suppress_reasons:
                    event["event_type"] = "MANAGE"
                    event["event_reason"] = "hold_model_policy"
                    if isinstance(event.get("position"), dict):
                        position_state = dict(event["position"])
                    event_type = "MANAGE"

            # Runtime halt handling:
            # - If halted and flat, suppress entries.
            # - If halted and in-position, force flatten on next bar.
            if runtime_halted:
                if isinstance(book.open_position, dict):
                    halt_side = str(book.open_position.get("side", "")).upper().strip()
                    halt_price = _entry_price_for_side(halt_side, last_ce_price, last_pe_price)
                    if halt_side in {"CE", "PE"} and np.isfinite(halt_price) and halt_price > 0:
                        halt_snapshot = dict(book.open_position)
                        pos_ref = event.get("position")
                        held_minutes = 0
                        if isinstance(pos_ref, dict):
                            try:
                                held_minutes = int(
                                    (pd.Timestamp(str(event.get("timestamp"))) - pd.Timestamp(str(pos_ref.get("entry_timestamp"))))
                                    / pd.Timedelta(minutes=1)
                                )
                            except Exception:
                                held_minutes = 0
                        _apply_exit(book, side=halt_side, price=halt_price, fee_fraction=fee_fraction)
                        trade_pnl = _register_exit_pnl()
                        position_state = None
                        forced_halt_event = {
                            "generated_at": str(event.get("generated_at") or pd.Timestamp.now(tz="Asia/Kolkata").isoformat()),
                            "timestamp": str(event.get("timestamp")),
                            "trade_date": str(event.get("trade_date", "")),
                            "mode": mode,
                            "ce_prob": _safe_float(event.get("ce_prob")),
                            "pe_prob": _safe_float(event.get("pe_prob")),
                            "ce_threshold": float(thresholds.ce),
                            "pe_threshold": float(thresholds.pe),
                            "action": "HOLD",
                            "confidence": _safe_float(event.get("confidence")),
                            "event_type": "EXIT",
                            "event_reason": "runtime_guard_halt",
                            "held_minutes": int(max(0, held_minutes)),
                            "position": dict(pos_ref) if isinstance(pos_ref, dict) else {"side": halt_side, "entry_timestamp": str(halt_snapshot.get("entry_timestamp", ""))},
                            "source": "redis_pubsub",
                            "instrument": instrument,
                        }
                        _record_event(
                            forced_halt_event,
                            risk_details={"reason": str(runtime_halt_reason or "runtime_guard_halt")},
                            exit_position_snapshot=halt_snapshot,
                            side_hint=halt_side,
                            trade_pnl=trade_pnl,
                        )
                        last_ts = emitted_ts
                        bars_processed += 1
                        if max_iterations is not None and bars_processed >= int(max_iterations):
                            break
                        continue
                else:
                    event["event_type"] = "IDLE"
                    event["event_reason"] = "runtime_guard_halt"
                    event["action"] = "HOLD"
                    event["position"] = None
                    position_state = None
                    event_type = "IDLE"

            trade_pnl: Optional[float] = None
            if event_type == "ENTRY":
                block_reason = _entry_block_reason(event)
                if current_expiry_mismatch:
                    block_reason = "expiry_mismatch_context"
                if block_reason:
                    event["event_type"] = "IDLE"
                    event["event_reason"] = str(block_reason)
                    event["action"] = "HOLD"
                    event["position"] = None
                    event_type = "IDLE"
                    position_state = None
                else:
                    side = "CE" if str(event.get("action")) == "BUY_CE" else ("PE" if str(event.get("action")) == "BUY_PE" else side)
                    entry_price = _entry_price_for_side(side, last_ce_price, last_pe_price)
                    option_symbol = _build_option_symbol(instrument=instrument, expiry_code=expiry_code, strike=atm_strike, side=side)
                    opened = _apply_entry(
                        book,
                        side=side,
                        price=entry_price,
                        ts=str(event.get("timestamp")),
                        fee_fraction=fee_fraction,
                        risk_cfg=risk_cfg,
                        atm_strike=atm_strike,
                        expiry_code=expiry_code,
                        option_symbol=option_symbol,
                    )
                    if not opened:
                        event["event_type"] = "IDLE"
                        event["event_reason"] = "entry_price_unavailable"
                        event["position"] = None
                        event_type = "IDLE"
                        position_state = None
                    else:
                        entries_taken_total += 1
                        entry_trade_date = str(event.get("trade_date") or "").strip()
                        entry_ts = _parse_event_timestamp(event.get("timestamp"))
                        if not entry_trade_date and entry_ts is not None:
                            entry_trade_date = str(entry_ts.date())
                        if entry_trade_date:
                            daily_entries[entry_trade_date] = int(daily_entries.get(entry_trade_date, 0)) + 1
                        if entry_ts is not None:
                            last_entry_timestamp = entry_ts
            elif event_type == "EXIT":
                pos = event.get("position")
                exit_side = str(pos.get("side")) if isinstance(pos, dict) else side
                exit_price = _entry_price_for_side(exit_side, last_ce_price, last_pe_price)
                if isinstance(book.open_position, dict):
                    exit_position_snapshot = dict(book.open_position)
                _apply_exit(book, side=exit_side, price=exit_price, fee_fraction=fee_fraction)
                trade_pnl = _register_exit_pnl()

            _record_event(
                event,
                risk_details=None,
                exit_position_snapshot=exit_position_snapshot,
                side_hint=side,
                trade_pnl=trade_pnl,
            )
            last_ts = emitted_ts
            bars_processed += 1
            if max_iterations is not None and bars_processed >= int(max_iterations):
                break
    finally:
        try:
            pubsub.close()
        except Exception:
            pass

    if position_state is not None and last_ts:
        forced_exit = _build_session_end_event(
            mode=mode,
            thresholds=thresholds,
            last_timestamp=pd.Timestamp(last_ts),
            position=position_state,
        )
        side = str(position_state.get("side", ""))
        exit_price = _entry_price_for_side(side, last_ce_price, last_pe_price)
        exit_snapshot = dict(book.open_position) if isinstance(book.open_position, dict) else None
        _apply_exit(book, side=side, price=exit_price, fee_fraction=fee_fraction)
        trade_pnl = _register_exit_pnl()
        forced_exit["source"] = "redis_pubsub"
        forced_exit["instrument"] = instrument
        _record_event(
            forced_exit,
            risk_details={"reason": "session_end"},
            exit_position_snapshot=exit_snapshot,
            side_hint=side,
            trade_pnl=trade_pnl,
        )

    final_mtm = book.mark_to_market(ce_price=last_ce_price, pe_price=last_pe_price)
    event_counts: Dict[str, int] = {}
    event_reason_counts: Dict[str, int] = {}
    for item in events:
        event_type = str(item.get("event_type"))
        event_reason = str(item.get("event_reason"))
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        event_reason_counts[event_reason] = event_reason_counts.get(event_reason, 0) + 1
    return {
        "mode": mode,
        "instrument": instrument,
        "messages_total": int(messages_total),
        "bars_processed": int(bars_processed),
        "events_emitted": int(len(events)),
        "event_counts": event_counts,
        "event_reason_counts": event_reason_counts,
        "trades_closed": int(book.trades_closed),
        "initial": {
            "ce_capital": float(initial_ce_capital),
            "pe_capital": float(initial_pe_capital),
            "total_capital": float(initial_ce_capital + initial_pe_capital),
        },
        "final": {
            "ce_capital_mtm": float(final_mtm["ce_capital_mtm"]),
            "pe_capital_mtm": float(final_mtm["pe_capital_mtm"]),
            "total_capital_mtm": float(final_mtm["total_capital_mtm"]),
            "ce_capital_realized": float(book.ce_capital),
            "pe_capital_realized": float(book.pe_capital),
        },
        "subscriptions": {
            "ohlc_pattern": ohlc_subscription,
            "options_channel": options_subscription,
            "depth_channel": depth_subscription,
        },
        "fee_bps": float(fee_bps),
        "risk_config": {
            "stop_loss_pct": float(risk_cfg.stop_loss_pct),
            "trailing_enabled": bool(risk_cfg.trailing_enabled),
            "trailing_activation_pct": float(risk_cfg.trailing_activation_pct),
            "trailing_offset_pct": float(risk_cfg.trailing_offset_pct),
            "trailing_lock_breakeven": bool(risk_cfg.trailing_lock_breakeven),
            "stagnation_enabled": bool(risk_cfg.stagnation_enabled),
            "stagnation_window_minutes": int(risk_cfg.stagnation_window_minutes),
            "stagnation_threshold_pct": float(risk_cfg.stagnation_threshold_pct),
            "stagnation_volatility_multiplier": float(risk_cfg.stagnation_volatility_multiplier),
            "stagnation_min_hold_minutes": int(risk_cfg.stagnation_min_hold_minutes),
            "model_exit_policy": str(risk_cfg.model_exit_policy),
            "stop_execution_mode": str(risk_cfg.stop_execution_mode),
            "stop_limit_offset_pct": float(risk_cfg.stop_limit_offset_pct),
            "stop_limit_max_wait_events": int(risk_cfg.stop_limit_max_wait_events),
            "option_lot_size": float(option_lot_size),
        },
        "runtime_guard_config": {
            "max_consecutive_losses": int(guard_cfg.max_consecutive_losses) if guard_cfg.max_consecutive_losses is not None else None,
            "max_drawdown_pct": float(guard_cfg.max_drawdown_pct),
        },
        "runtime_guard_state": {
            "is_halted": bool(runtime_halted),
            "halt_reason": str(runtime_halt_reason) if runtime_halt_reason else None,
            "halt_timestamp": str(runtime_halt_timestamp) if runtime_halt_timestamp else None,
            "consecutive_loss_exits": int(consecutive_loss_exits),
            "max_drawdown_observed": float(max_drawdown_observed),
        },
        "quality_policy_config": {
            "max_entries_per_day": (
                int(quality_cfg.max_entries_per_day) if quality_cfg.max_entries_per_day is not None else None
            ),
            "entry_cutoff_hour": int(quality_cfg.entry_cutoff_hour) if quality_cfg.entry_cutoff_hour is not None else None,
            "entry_cooldown_minutes": int(quality_cfg.entry_cooldown_minutes),
            "min_side_prob": float(quality_cfg.min_side_prob),
            "min_prob_edge": float(quality_cfg.min_prob_edge),
            "skip_weekdays": [int(x) for x in quality_cfg.skip_weekdays],
        },
        "quality_policy_state": {
            "entries_taken_total": int(entries_taken_total),
            "days_with_entries": int(len(daily_entries)),
        },
        "output_jsonl": str(output_jsonl) if output_jsonl is not None else None,
        "feature_trace_jsonl": str(feature_trace_jsonl) if feature_trace_jsonl is not None else None,
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Paper capital runner using live Redis inference loop")
    parser.add_argument("--model-package", default="ml_pipeline/artifacts/t06_baseline_model.joblib")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t08_threshold_report.json")
    parser.add_argument(
        "--ce-threshold",
        type=float,
        default=None,
        help="Optional CE probability threshold override (0-1).",
    )
    parser.add_argument(
        "--pe-threshold",
        type=float,
        default=None,
        help="Optional PE probability threshold override (0-1).",
    )
    parser.add_argument("--mode", default="dual", choices=["dual", "ce_only", "pe_only"])
    parser.add_argument("--instrument", default="BANKNIFTY-I")
    parser.add_argument("--initial-ce-capital", type=float, default=1000.0)
    parser.add_argument("--initial-pe-capital", type=float, default=1000.0)
    parser.add_argument("--fee-bps", type=float, default=0.0)
    parser.add_argument("--output-jsonl", default="ml_pipeline/artifacts/t33_paper_capital_events.jsonl")
    parser.add_argument(
        "--feature-trace-jsonl",
        default="",
        help="Optional JSONL path to store per-bar model input feature vectors and probabilities",
    )
    parser.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "localhost"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.getenv("REDIS_DB", "0")))
    parser.add_argument("--redis-password", default=os.getenv("REDIS_PASSWORD"))
    parser.add_argument("--redis-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--ohlc-pattern", default=None)
    parser.add_argument("--options-channel", default=None)
    parser.add_argument("--depth-channel", default=None)
    parser.add_argument("--max-iterations", type=int, default=120)
    parser.add_argument("--max-hold-minutes", type=int, default=5)
    parser.add_argument("--confidence-buffer", type=float, default=0.05)
    parser.add_argument("--max-idle-seconds", type=float, default=120.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.0, help="Hard stop loss percentage (e.g. 8 = 8%%)")
    parser.add_argument("--trailing-enabled", action="store_true", help="Enable trailing stop updates once in profit")
    parser.add_argument(
        "--trailing-activation-pct",
        type=float,
        default=10.0,
        help="Activate trailing once position gains this percentage (e.g. 10 = +10%%)",
    )
    parser.add_argument(
        "--trailing-offset-pct",
        type=float,
        default=5.0,
        help="Trailing stop offset from high-water mark in percentage (e.g. 5 = 5%%)",
    )
    parser.add_argument("--no-trailing-lock-breakeven", action="store_true")
    parser.add_argument("--stagnation-enabled", action="store_true", help="Enable low-movement stagnation exit overlay")
    parser.add_argument(
        "--stagnation-window-minutes",
        type=int,
        default=10,
        help="Bars/minutes window used to evaluate stagnation exit",
    )
    parser.add_argument(
        "--stagnation-threshold-pct",
        type=float,
        default=0.8,
        help="Base stagnation threshold in percentage of entry price (e.g. 0.8 = 0.8%%)",
    )
    parser.add_argument(
        "--stagnation-volatility-multiplier",
        type=float,
        default=2.0,
        help="Adaptive multiplier applied to median step pct within the stagnation window",
    )
    parser.add_argument(
        "--stagnation-min-hold-minutes",
        type=int,
        default=0,
        help="Minimum hold before stagnation exits are allowed",
    )
    parser.add_argument(
        "--model-exit-policy",
        default="strict",
        choices=["strict", "signal_only", "stop_only", "training_parity"],
    )
    parser.add_argument("--stop-execution-mode", default="stop_market", choices=["stop_market", "stop_limit"])
    parser.add_argument(
        "--stop-limit-offset-pct",
        type=float,
        default=0.2,
        help="Stop-limit offset percentage below stop trigger for sell exits (e.g. 0.2 = 0.2%%)",
    )
    parser.add_argument(
        "--stop-limit-max-wait-events",
        type=int,
        default=3,
        help="Max incoming events to wait for stop-limit fill before fallback market exit",
    )
    parser.add_argument(
        "--runtime-guard-max-consecutive-losses",
        type=int,
        default=0,
        help="Halt new trading after this many consecutive losing exits (0 disables).",
    )
    parser.add_argument(
        "--runtime-guard-max-drawdown-pct",
        type=float,
        default=0.0,
        help="Halt new trading when MTM drawdown reaches this percentage (0 disables).",
    )
    parser.add_argument(
        "--quality-max-entries-per-day",
        type=int,
        default=0,
        help="Maximum new entries per trade day (0 disables).",
    )
    parser.add_argument(
        "--quality-entry-cutoff-hour",
        type=int,
        default=-1,
        help="Block new entries at/after this hour in exchange time (0-23, -1 disables).",
    )
    parser.add_argument(
        "--quality-entry-cooldown-minutes",
        type=int,
        default=0,
        help="Minimum minutes between new entries (0 disables).",
    )
    parser.add_argument(
        "--quality-min-side-prob",
        type=float,
        default=0.0,
        help="Minimum side probability required for a new entry (0-1).",
    )
    parser.add_argument(
        "--quality-min-prob-edge",
        type=float,
        default=0.0,
        help="Minimum (side_prob - other_prob) required for a new entry (0-1).",
    )
    parser.add_argument(
        "--quality-skip-weekdays",
        default="",
        help="Comma-separated weekdays to skip entries, e.g. 'wed' or '2,5'. 0=Mon..6=Sun.",
    )
    parser.add_argument("--option-lot-size", type=float, default=15.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        quality_skip_weekdays = _parse_weekday_tokens(str(args.quality_skip_weekdays or ""))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    model_path = Path(args.model_package)
    threshold_path = Path(args.threshold_report)
    if not model_path.exists():
        print(f"ERROR: model package not found: {model_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2
    model_package = load_model_package(model_path)
    loaded_thresholds = load_thresholds(threshold_path)
    ce_threshold = float(loaded_thresholds.ce)
    pe_threshold = float(loaded_thresholds.pe)
    if args.ce_threshold is not None:
        ce_override = float(args.ce_threshold)
        if ce_override < 0.0 or ce_override > 1.0:
            print(f"ERROR: --ce-threshold must be within [0, 1], got {ce_override}")
            return 2
        ce_threshold = ce_override
    if args.pe_threshold is not None:
        pe_override = float(args.pe_threshold)
        if pe_override < 0.0 or pe_override > 1.0:
            print(f"ERROR: --pe-threshold must be within [0, 1], got {pe_override}")
            return 2
        pe_threshold = pe_override
    thresholds = DecisionThresholds(
        ce=ce_threshold,
        pe=pe_threshold,
        cost_per_trade=loaded_thresholds.cost_per_trade,
    )

    out = Path(args.output_jsonl) if args.output_jsonl else None
    trace_out = Path(args.feature_trace_jsonl) if str(args.feature_trace_jsonl or "").strip() else None
    summary = run_live_redis_capital_loop(
        instrument=str(args.instrument),
        model_package=model_package,
        thresholds=thresholds,
        initial_ce_capital=float(args.initial_ce_capital),
        initial_pe_capital=float(args.initial_pe_capital),
        output_jsonl=out,
        feature_trace_jsonl=trace_out,
        mode=str(args.mode),
        redis_host=str(args.redis_host),
        redis_port=int(args.redis_port),
        redis_db=int(args.redis_db),
        redis_password=args.redis_password,
        redis_timeout_seconds=float(args.redis_timeout_seconds),
        ohlc_pattern=args.ohlc_pattern,
        options_channel=args.options_channel,
        depth_channel=args.depth_channel,
        max_iterations=int(args.max_iterations) if args.max_iterations is not None else None,
        max_hold_minutes=int(args.max_hold_minutes),
        confidence_buffer=float(args.confidence_buffer),
        fee_bps=float(args.fee_bps),
        max_idle_seconds=float(args.max_idle_seconds),
        stop_loss_pct=float(max(0.0, args.stop_loss_pct) / 100.0),
        trailing_enabled=bool(args.trailing_enabled),
        trailing_activation_pct=float(max(0.0, args.trailing_activation_pct) / 100.0),
        trailing_offset_pct=float(max(0.0, args.trailing_offset_pct) / 100.0),
        trailing_lock_breakeven=(not bool(args.no_trailing_lock_breakeven)),
        stagnation_enabled=bool(args.stagnation_enabled),
        stagnation_window_minutes=int(max(2, args.stagnation_window_minutes)),
        stagnation_threshold_pct=float(max(0.0, args.stagnation_threshold_pct) / 100.0),
        stagnation_volatility_multiplier=float(max(0.0, args.stagnation_volatility_multiplier)),
        stagnation_min_hold_minutes=int(max(0, args.stagnation_min_hold_minutes)),
        model_exit_policy=str(args.model_exit_policy),
        stop_execution_mode=str(args.stop_execution_mode),
        stop_limit_offset_pct=float(max(0.0, args.stop_limit_offset_pct) / 100.0),
        stop_limit_max_wait_events=int(max(1, args.stop_limit_max_wait_events)),
        option_lot_size=float(args.option_lot_size),
        runtime_guard_max_consecutive_losses=(
            int(args.runtime_guard_max_consecutive_losses)
            if int(args.runtime_guard_max_consecutive_losses) > 0
            else None
        ),
        runtime_guard_max_drawdown_pct=float(max(0.0, args.runtime_guard_max_drawdown_pct) / 100.0),
        quality_max_entries_per_day=(
            int(args.quality_max_entries_per_day) if int(args.quality_max_entries_per_day) > 0 else None
        ),
        quality_entry_cutoff_hour=(
            int(args.quality_entry_cutoff_hour)
            if int(args.quality_entry_cutoff_hour) >= 0
            else None
        ),
        quality_entry_cooldown_minutes=int(max(0, args.quality_entry_cooldown_minutes)),
        quality_min_side_prob=float(min(1.0, max(0.0, args.quality_min_side_prob))),
        quality_min_prob_edge=float(min(1.0, max(0.0, args.quality_min_prob_edge))),
        quality_skip_weekdays=quality_skip_weekdays,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
