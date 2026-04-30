from __future__ import annotations

import bisect
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, MongoClient

try:
    from .schemas.monitor import (
        MonitorAlert,
        MonitorCandle,
        MonitorSession,
        MonitorSignal,
        MonitorSignalMetrics,
        MonitorTrade,
    )
except ImportError:
    from schemas.monitor import (  # type: ignore
        MonitorAlert,
        MonitorCandle,
        MonitorSession,
        MonitorSignal,
        MonitorSignalMetrics,
        MonitorTrade,
    )

_IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, fallback: Optional[float] = 0.0) -> Optional[float]:
    try:
        out = float(v)
        if math.isnan(out) or math.isinf(out):
            return fallback
        return out
    except Exception:
        return fallback


def _ts_ms(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(text[:26], fmt[:len(fmt)])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _fmt_time(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=_IST)
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _fmt_hold(entry_ms: int, exit_ms: int) -> str:
    s = max(0, int((exit_ms - entry_ms) / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _nearest_candle_idx(candle_ts_sorted: List[int], ts_ms: int) -> int:
    if not candle_ts_sorted:
        return 0
    pos = bisect.bisect_right(candle_ts_sorted, ts_ms)
    if pos == 0:
        return 0
    if pos == len(candle_ts_sorted):
        return len(candle_ts_sorted) - 1
    before = pos - 1
    if abs(candle_ts_sorted[before] - ts_ms) <= abs(candle_ts_sorted[pos] - ts_ms):
        return before
    return pos


def _today_ist() -> str:
    return datetime.now(tz=_IST).strftime("%Y-%m-%d")


# ── Transforms ─────────────────────────────────────────────────────────────────

def _snapshot_to_candle(doc: Dict[str, Any], idx: int) -> Optional[MonitorCandle]:
    payload = doc.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    snapshot = payload.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        return None
    futures_bar = snapshot.get("futures_bar") or {}
    if not isinstance(futures_bar, dict):
        return None

    close = _safe_float(futures_bar.get("fut_close"), fallback=None)
    if close is None:
        return None

    open_ = _safe_float(futures_bar.get("fut_open"), fallback=close) or close
    high = _safe_float(futures_bar.get("fut_high"), fallback=max(open_, close)) or max(open_, close)
    low = _safe_float(futures_bar.get("fut_low"), fallback=min(open_, close)) or min(open_, close)
    volume = int(_safe_float(futures_bar.get("fut_volume"), fallback=0.0) or 0.0)

    session_ctx = snapshot.get("session_context") or {}
    if not isinstance(session_ctx, dict):
        session_ctx = {}

    raw_ts = doc.get("timestamp") or session_ctx.get("timestamp")
    ts = _ts_ms(raw_ts)
    if ts is None:
        return None

    label = str(session_ctx.get("time") or "").strip() or _fmt_time(ts)

    return MonitorCandle(
        i=idx,
        o=round(open_, 2),
        h=round(high, 2),
        l=round(low, 2),
        c=round(close, 2),
        v=volume,
        t=ts,
        label=label,
    )


def _option_dir_to_bias(raw: str) -> str:
    """Map options direction (PE/CE/AVOID) to chart bias (SHORT/LONG/LONG)."""
    v = str(raw or "").strip().upper()
    if v == "PE":
        return "SHORT"
    if v == "CE":
        return "LONG"
    if v in ("LONG", "SHORT"):
        return v
    return "LONG"


def _doc_reason_detail(doc: Dict[str, Any], payload_signal: Dict[str, Any]) -> str:
    return str(doc.get("reason") or payload_signal.get("reason") or "").strip()


def _vote_to_signal(
    doc: Dict[str, Any],
    candle_ts_sorted: List[int],
) -> Optional[MonitorSignal]:
    payload = doc.get("payload") or {}
    payload_signal = (payload.get("signal") or {}) if isinstance(payload, dict) else {}
    if not isinstance(payload_signal, dict):
        payload_signal = {}

    raw_ts = doc.get("timestamp") or payload_signal.get("timestamp")
    ts = _ts_ms(raw_ts)
    if ts is None:
        return None

    # Skip AVOID direction — no trade bias
    raw_dir = str(doc.get("direction") or payload_signal.get("direction") or "").strip().upper()
    if raw_dir == "AVOID":
        return None

    idx = _nearest_candle_idx(candle_ts_sorted, ts)
    direction = _option_dir_to_bias(raw_dir)

    conf = max(0.0, min(1.0, _safe_float(
        doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence"),
        fallback=0.5,
    ) or 0.5))

    regime = str(doc.get("regime") or payload_signal.get("regime") or "UNKNOWN").strip()

    signal_type = str(
        doc.get("signal_type") or payload_signal.get("signal_type") or ""
    ).strip().upper()
    reason_code = str(
        doc.get("decision_reason_code") or payload_signal.get("decision_reason_code") or ""
    ).strip()
    # fired = engine decided to enter; SKIP = evaluated but not fired
    fired = signal_type == "ENTRY"

    strat = str(doc.get("strategy") or payload_signal.get("strategy") or "unknown").strip()

    # ML metrics are top-level fields in the document (not nested in decision_metrics)
    def _p(key: str, default: float = 0.5) -> float:
        v = _safe_float(doc.get(key) if doc.get(key) is not None else (
            (doc.get("decision_metrics") or {}).get(key) if isinstance(doc.get("decision_metrics"), dict) else None
        ), default)
        return max(0.0, min(1.0, v or default))

    metrics = MonitorSignalMetrics(
        entry_prob=_p("ml_entry_prob"),
        trade_prob=_p("direction_trade_prob", _p("ml_entry_prob")),
        up_prob=_p("ml_direction_up_prob"),
        ce_prob=_p("ml_ce_prob"),
        pe_prob=_p("ml_pe_prob"),
        recipe_prob=_p("ml_recipe_prob"),
        recipe_margin=max(0.0, min(1.0, _safe_float(
            doc.get("ml_recipe_margin") if doc.get("ml_recipe_margin") is not None else
            (doc.get("decision_metrics") or {}).get("ml_recipe_margin"), 0.0
        ) or 0.0)),
    )

    return MonitorSignal(
        t=ts,
        idx=idx,
        strat=strat or "unknown",
        dir=direction,
        conf=round(conf, 4),
        fired=fired,
        reason=reason_code or "UNKNOWN",
        detail=_doc_reason_detail(doc, payload_signal),
        metrics=metrics,
        regime=regime or "UNKNOWN",
    )


def _trade_signal_to_signal(
    doc: Dict[str, Any],
    candle_ts_sorted: List[int],
) -> Optional[MonitorSignal]:
    payload = doc.get("payload") or {}
    payload_signal = (payload.get("signal") or {}) if isinstance(payload, dict) else {}
    if not isinstance(payload_signal, dict):
        payload_signal = {}

    signal_type = str(doc.get("signal_type") or payload_signal.get("signal_type") or "").strip().upper()
    raw_dir = str(doc.get("direction") or payload_signal.get("direction") or "").strip().upper()
    if signal_type == "EXIT" or raw_dir in ("", "AVOID", "EXIT"):
        return None

    raw_ts = doc.get("timestamp") or payload_signal.get("timestamp")
    ts = _ts_ms(raw_ts)
    if ts is None:
        return None

    idx = _nearest_candle_idx(candle_ts_sorted, ts)
    direction = _option_dir_to_bias(raw_dir)
    regime = str(doc.get("regime") or payload_signal.get("regime") or "UNKNOWN").strip()
    conf = max(0.0, min(1.0, _safe_float(
        doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence"),
        fallback=0.5,
    ) or 0.5))
    reason_code = str(
        doc.get("decision_reason_code") or payload_signal.get("decision_reason_code") or signal_type or "UNKNOWN"
    ).strip()
    strat = str(
        doc.get("entry_strategy_name")
        or payload_signal.get("entry_strategy_name")
        or doc.get("strategy")
        or payload_signal.get("strategy")
        or "unknown"
    ).strip()

    def _p(key: str, default: float = 0.5) -> float:
        v = _safe_float(doc.get(key) if doc.get(key) is not None else (
            (doc.get("decision_metrics") or {}).get(key) if isinstance(doc.get("decision_metrics"), dict) else None
        ), default)
        return max(0.0, min(1.0, v or default))

    return MonitorSignal(
        t=ts,
        idx=idx,
        strat=strat or "unknown",
        dir=direction,
        conf=round(conf, 4),
        fired=signal_type == "ENTRY",
        reason=reason_code or "UNKNOWN",
        detail=_doc_reason_detail(doc, payload_signal),
        metrics=MonitorSignalMetrics(
            entry_prob=_p("ml_entry_prob"),
            trade_prob=_p("direction_trade_prob", _p("ml_entry_prob")),
            up_prob=_p("ml_direction_up_prob"),
            ce_prob=_p("ml_ce_prob"),
            pe_prob=_p("ml_pe_prob"),
            recipe_prob=_p("ml_recipe_prob"),
            recipe_margin=max(0.0, min(1.0, _safe_float(
                doc.get("ml_recipe_margin") if doc.get("ml_recipe_margin") is not None else
                (doc.get("decision_metrics") or {}).get("ml_recipe_margin"), 0.0
            ) or 0.0)),
        ),
        regime=regime or "UNKNOWN",
    )


def _underlying_stop_level(open_pos: Dict[str, Any]) -> Optional[float]:
    """Compute the underlying futures stop price for display when no premium stop_price is set."""
    underlying_stop_pct = _safe_float(open_pos.get("underlying_stop_pct"), fallback=None)
    entry_futures = _safe_float(open_pos.get("entry_futures_price"), fallback=None)
    if underlying_stop_pct is None or entry_futures is None or underlying_stop_pct <= 0:
        return None
    direction = str(open_pos.get("direction") or "").strip().upper()
    if direction in ("CE", "LONG"):
        return round(entry_futures * (1.0 - underlying_stop_pct), 2)
    if direction in ("PE", "SHORT"):
        return round(entry_futures * (1.0 + underlying_stop_pct), 2)
    return None


def _stop_basis(open_pos: Dict[str, Any]) -> Optional[str]:
    underlying_stop_pct = _safe_float(open_pos.get("underlying_stop_pct"), fallback=None)
    entry_futures = _safe_float(open_pos.get("entry_futures_price"), fallback=None)
    if underlying_stop_pct is not None and underlying_stop_pct > 0 and entry_futures is not None and entry_futures > 0:
        return "underlying"
    premium_stop_pct = _safe_float(open_pos.get("stop_loss_pct"), fallback=None)
    premium_stop_price = _safe_float(open_pos.get("stop_price"), fallback=None)
    if (premium_stop_pct is not None and premium_stop_pct > 0) or (premium_stop_price is not None and premium_stop_price > 0):
        return "premium"
    return None


def _find_underlying_stop_trigger(
    *,
    direction: str,
    stop_level: Optional[float],
    entry_idx: int,
    exit_idx: int,
    candles: List[MonitorCandle],
) -> tuple[Optional[str], str]:
    if stop_level is None or entry_idx < 0 or exit_idx < entry_idx:
        return None, ""
    dir_norm = str(direction or "").strip().upper()
    intrabar_hit: Optional[MonitorCandle] = None
    close_hit: Optional[MonitorCandle] = None
    comparison = "<=" if dir_norm in ("CE", "LONG") else ">="
    for idx in range(entry_idx, min(exit_idx, len(candles) - 1) + 1):
        candle = candles[idx]
        if dir_norm in ("CE", "LONG"):
            if intrabar_hit is None and candle.l <= stop_level:
                intrabar_hit = candle
            if close_hit is None and candle.c <= stop_level:
                close_hit = candle
        elif dir_norm in ("PE", "SHORT"):
            if intrabar_hit is None and candle.h >= stop_level:
                intrabar_hit = candle
            if close_hit is None and candle.c >= stop_level:
                close_hit = candle
    trigger = close_hit or intrabar_hit
    trigger_label = trigger.label if trigger is not None else None
    if close_hit is not None and intrabar_hit is not None and close_hit.i != intrabar_hit.i:
        detail = (
            f"underlying stop on close ({comparison} {stop_level:.2f}) at {close_hit.label}; "
            f"first intrabar breach at {intrabar_hit.label}"
        )
    elif close_hit is not None:
        detail = f"underlying stop on close ({comparison} {stop_level:.2f}) at {close_hit.label}"
    elif intrabar_hit is not None:
        detail = f"underlying stop breached intrabar ({comparison} {stop_level:.2f}) at {intrabar_hit.label}"
    else:
        detail = f"underlying stop level {comparison} {stop_level:.2f}"
    return trigger_label, detail


def _position_to_trade(
    position_id: str,
    open_pos: Dict[str, Any],
    close_pos: Dict[str, Any],
    open_doc: Dict[str, Any],
    close_doc: Dict[str, Any],
    signal: Optional[MonitorSignal],
    candle_ts_sorted: List[int],
    candles: List[MonitorCandle],
) -> Optional[MonitorTrade]:
    entry_ts = _ts_ms(open_pos.get("timestamp") or open_doc.get("timestamp"))
    exit_ts = _ts_ms(close_pos.get("timestamp") or close_doc.get("timestamp"))
    if entry_ts is None or exit_ts is None:
        return None

    entry_idx = _nearest_candle_idx(candle_ts_sorted, entry_ts)
    exit_idx = _nearest_candle_idx(candle_ts_sorted, exit_ts)

    entry_px = _safe_float(open_pos.get("entry_premium"), fallback=None)
    entry_px = float(entry_px) if entry_px is not None else candles[entry_idx].c
    exit_px = _safe_float(close_pos.get("exit_premium"), fallback=None)
    exit_px = float(exit_px) if exit_px is not None else candles[exit_idx].c

    pnl_pct = float(_safe_float(close_pos.get("pnl_pct"), fallback=0.0) or 0.0)

    direction = _option_dir_to_bias(str(open_pos.get("direction") or "LONG"))

    contrib = open_pos.get("contributing_strategies")
    if isinstance(contrib, list) and contrib:
        strat = str(contrib[0] or "unknown").strip()
    else:
        strat = str(open_pos.get("entry_strategy") or open_pos.get("strategy") or "unknown").strip()
    stop_basis = _stop_basis(open_pos)
    entry_futures_price = _safe_float(open_pos.get("entry_futures_price"), fallback=None)
    underlying_stop_price = _underlying_stop_level(open_pos)
    stop_trigger_candle = None
    stop_trigger_detail = ""
    if stop_basis == "underlying":
        stop_trigger_candle, stop_trigger_detail = _find_underlying_stop_trigger(
            direction=str(open_pos.get("direction") or ""),
            stop_level=underlying_stop_price,
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            candles=candles,
        )

    if signal is None:
        signal = MonitorSignal(
            t=entry_ts, idx=entry_idx, strat=strat or "unknown", dir=direction,
            conf=0.5, fired=True, reason="ENTRY_MET",
            detail="Synthetic fallback built from position lifecycle because no entry vote/signal row was linked.",
            metrics=MonitorSignalMetrics(
                entry_prob=0.5, trade_prob=0.5, up_prob=0.5,
                ce_prob=0.5, pe_prob=0.5, recipe_prob=0.5, recipe_margin=0.0,
            ),
            regime="UNKNOWN",
        )

    return MonitorTrade(
        id=position_id,
        t=entry_ts,
        tLabel=_fmt_time(entry_ts),
        strat=strat or signal.strat,
        dir=direction,
        qty=int(_safe_float(open_pos.get("lots") or open_pos.get("qty"), fallback=1) or 1),
        entry=round(entry_px, 2),
        exit=round(exit_px, 2),
        entryIdx=entry_idx,
        exitIdx=exit_idx,
        pnlPct=round(pnl_pct, 2),
        hold=_fmt_hold(entry_ts, exit_ts),
        signal=signal,
        entryReason=signal.reason,
        entryDetail=signal.detail,
        exitReason=str(close_pos.get("exit_reason") or close_doc.get("exit_reason") or "").strip(),
        exitDetail=str(close_pos.get("reason") or close_doc.get("reason") or "").strip(),
        stopLossPct=_safe_float(open_pos.get("underlying_stop_pct") or open_pos.get("stop_loss_pct"), fallback=None),
        targetPct=_safe_float(open_pos.get("underlying_target_pct") or open_pos.get("target_pct"), fallback=None),
        maxHoldBars=int(open_pos.get("max_hold_bars")) if open_pos.get("max_hold_bars") is not None else None,
        stopPrice=_safe_float(close_pos.get("stop_price"), fallback=_safe_float(open_pos.get("stop_price"), fallback=_underlying_stop_level(open_pos))),
        stopBasis=stop_basis,
        entryFuturesPrice=entry_futures_price,
        underlyingStopPrice=underlying_stop_price,
        stopTriggerCandle=stop_trigger_candle,
        stopTriggerDetail=stop_trigger_detail,
    )


# ── Shared build logic ─────────────────────────────────────────────────────────

def _latest_run_id_for_date(db: Any, trade_date: str) -> Optional[str]:
    """Return the run_id of the most recently submitted completed eval run for trade_date."""
    runs_coll = str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")
    try:
        doc = db[runs_coll].find_one(
            {
                "status": "completed",
                "date_from": {"$lte": trade_date},
                "date_to": {"$gte": trade_date},
            },
            {"run_id": 1},
            sort=[("_id", -1)],
        )
        if doc and doc.get("run_id"):
            return str(doc["run_id"]).strip() or None
    except Exception:
        pass
    return None


def _build_session(
    db: Any,
    trade_date: str,
    coll_snapshots: str,
    coll_votes: str,
    coll_signals: str,
    coll_positions: str,
    run_id: Optional[str] = None,
) -> MonitorSession:
    latest_run_id = run_id or _latest_run_id_for_date(db, trade_date)
    date_q: Dict[str, Any] = {"trade_date_ist": trade_date}
    run_date_q: Dict[str, Any] = {"trade_date_ist": trade_date}
    if latest_run_id:
        run_date_q["run_id"] = latest_run_id

    snap_proj = {
        "_id": 0,
        "instrument": 1,
        "timestamp": 1,
        "payload.snapshot.session_context.timestamp": 1,
        "payload.snapshot.session_context.time": 1,
        "payload.snapshot.futures_bar.fut_open": 1,
        "payload.snapshot.futures_bar.fut_high": 1,
        "payload.snapshot.futures_bar.fut_low": 1,
        "payload.snapshot.futures_bar.fut_close": 1,
        "payload.snapshot.futures_bar.fut_volume": 1,
    }
    candles: List[MonitorCandle] = []
    instrument = "BANKNIFTY-I"
    for i, doc in enumerate(
        db[coll_snapshots].find(date_q, snap_proj).sort("timestamp", ASCENDING)
    ):
        c = _snapshot_to_candle(doc, i)
        if c is not None:
            candles.append(c)
            if instrument == "BANKNIFTY-I":
                val = str(doc.get("instrument") or "").strip()
                if val:
                    instrument = val

    if not candles:
        raise ValueError(f"No snapshot data for {trade_date} in {coll_snapshots}")

    candle_ts_sorted = [c.t for c in candles]

    # ── Read positions first so we know which signal_ids became trades ─────────
    pos_proj = {
        "_id": 0,
        "position_id": 1,
        "signal_id": 1,
        "event": 1,
        "timestamp": 1,
        "payload.position": 1,
        "run_id": 1,
    }
    position_map: Dict[str, Dict[str, Any]] = {}
    detected_run_id: Optional[str] = latest_run_id
    for doc in db[coll_positions].find(run_date_q, pos_proj).sort("timestamp", ASCENDING):
        pid = str(doc.get("position_id") or "").strip()
        if not pid:
            continue
        payload_pos = ((doc.get("payload") or {}).get("position") or {})
        if not isinstance(payload_pos, dict):
            payload_pos = {}
        event = str(doc.get("event") or payload_pos.get("event") or "").strip().upper()
        slot = position_map.setdefault(pid, {"position_id": pid})
        if event == "POSITION_OPEN":
            slot["open"] = payload_pos
            slot["open_doc"] = doc
            slot["signal_id"] = str(doc.get("signal_id") or payload_pos.get("signal_id") or "").strip()
        elif event == "POSITION_CLOSE":
            slot["close"] = payload_pos
            slot["close_doc"] = doc
            if not slot.get("signal_id"):
                slot["signal_id"] = str(doc.get("signal_id") or payload_pos.get("signal_id") or "").strip()
            if detected_run_id is None:
                detected_run_id = str(doc.get("run_id") or "").strip() or None

    # signal_ids that produced a fully closed position
    traded_signal_ids: set = {
        docs["signal_id"]
        for docs in position_map.values()
        if docs.get("signal_id") and isinstance(docs.get("open"), dict) and isinstance(docs.get("close"), dict)
    }

    # ── Now build signals, marking each as traded or skipped ─────────────────
    vote_proj = {
        "_id": 0,
        "signal_id": 1,
        "timestamp": 1,
        "signal_type": 1,
        "direction": 1,
        "confidence": 1,
        "regime": 1,
        "strategy": 1,
        "decision_metrics": 1,
        "decision_reason_code": 1,
        "payload.signal": 1,
    }
    signal_by_id: Dict[str, MonitorSignal] = {}
    signals: List[MonitorSignal] = []
    _seen_signal_keys: set = set()

    def _dedup_signal_key(doc: Dict[str, Any], sig: MonitorSignal) -> str:
        sid = str(doc.get("signal_id") or ((doc.get("payload") or {}).get("signal") or {}).get("signal_id") or "").strip()
        return sid if sid else f"{sig.idx}:{sig.dir}:{sig.strat}"

    def _extract_sid(doc: Dict[str, Any]) -> str:
        return str(doc.get("signal_id") or ((doc.get("payload") or {}).get("signal") or {}).get("signal_id") or "").strip()

    for doc in db[coll_votes].find(run_date_q, vote_proj).sort("timestamp", ASCENDING):
        sig = _vote_to_signal(doc, candle_ts_sorted)
        if sig is None:
            continue
        key = _dedup_signal_key(doc, sig)
        if key in _seen_signal_keys:
            continue
        _seen_signal_keys.add(key)
        sid = _extract_sid(doc)
        if sig.fired and sid:
            sig = sig.model_copy(update={"traded": sid in traded_signal_ids})
        signals.append(sig)
        if sid:
            signal_by_id[sid] = sig

    if not signals and coll_signals in db.list_collection_names():
        signal_proj = {
            "_id": 0,
            "signal_id": 1,
            "timestamp": 1,
            "signal_type": 1,
            "direction": 1,
            "confidence": 1,
            "regime": 1,
            "reason": 1,
            "decision_reason_code": 1,
            "decision_metrics": 1,
            "ml_entry_prob": 1,
            "ml_direction_up_prob": 1,
            "ml_ce_prob": 1,
            "ml_pe_prob": 1,
            "ml_recipe_prob": 1,
            "ml_recipe_margin": 1,
            "direction_trade_prob": 1,
            "entry_strategy_name": 1,
            "payload.signal": 1,
        }
        for doc in db[coll_signals].find(run_date_q, signal_proj).sort("timestamp", ASCENDING):
            sig = _trade_signal_to_signal(doc, candle_ts_sorted)
            if sig is None:
                continue
            key = _dedup_signal_key(doc, sig)
            if key in _seen_signal_keys:
                continue
            _seen_signal_keys.add(key)
            sid = _extract_sid(doc)
            if sig.fired and sid:
                sig = sig.model_copy(update={"traded": sid in traded_signal_ids})
            signals.append(sig)
            if sid:
                signal_by_id[sid] = sig

    trades: List[MonitorTrade] = []
    for pid, docs in position_map.items():
        if not isinstance(docs.get("open"), dict) or not isinstance(docs.get("close"), dict):
            continue
        sid = docs.get("signal_id", "")
        trade = _position_to_trade(
            pid,
            docs["open"],
            docs["close"],
            docs.get("open_doc") or {},
            docs.get("close_doc") or {},
            signal_by_id.get(sid) if sid else None,
            candle_ts_sorted,
            candles,
        )
        if trade is not None:
            trades.append(trade)

    # Deduplicate trades: identical (entryIdx, exitIdx, dir, strat, entry_px) → keep first occurrence
    _seen_trade_keys: set = set()
    deduped_trades: List[MonitorTrade] = []
    for tr in trades:
        tk = (tr.entryIdx, tr.exitIdx, tr.dir, tr.strat, tr.entry)
        if tk not in _seen_trade_keys:
            _seen_trade_keys.add(tk)
            deduped_trades.append(tr)
    trades = deduped_trades
    trades.sort(key=lambda t: t.entryIdx)

    alerts = [MonitorAlert(
        level="info", t="09:15",
        msg=f"<strong>Session loaded</strong> — {trade_date}",
        tms=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
    )]
    if not signals and not trades:
        alerts.append(MonitorAlert(
            level="warn", t="09:15",
            msg=(
                "Candles loaded, but no vote or position records exist for this date. "
                "Try a replay date with evaluations such as <strong>2024-01-23</strong>."
            ),
            tms=int(datetime.now(tz=timezone.utc).timestamp() * 1000) + 1,
        ))

    return MonitorSession(
        date=trade_date,
        instrument=instrument,
        candles=candles,
        signals=signals,
        trades=trades,
        alerts=alerts,
        basePrice=candles[0].c,
        runId=detected_run_id,
    )


# ── Sources ────────────────────────────────────────────────────────────────────

class MongoSource:
    """Historical replay — reads from *_historical collections."""

    COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
    COLL_VOTES = os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL", "strategy_votes_historical")
    COLL_SIGNALS = os.getenv("MONGO_COLL_TRADE_SIGNALS_HISTORICAL", "trade_signals_historical")
    COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL", "strategy_positions_historical")

    def __init__(self, db: Any, trade_date: str) -> None:
        self._db = db
        self._trade_date = trade_date
        self._session: Optional[MonitorSession] = None

    def get_session(self) -> MonitorSession:
        if self._session is None:
            self._session = _build_session(
                self._db, self._trade_date,
                self.COLL_SNAPSHOTS, self.COLL_VOTES, self.COLL_SIGNALS, self.COLL_POSITIONS,
            )
        return self._session


class LiveMongoSource:
    """Live session — reads from live (non-historical) collections and supports tick queries."""

    COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS", "phase1_market_snapshots")
    COLL_VOTES = os.getenv("MONGO_COLL_STRATEGY_VOTES", "strategy_votes")
    COLL_SIGNALS = os.getenv("MONGO_COLL_TRADE_SIGNALS", "trade_signals")
    COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS", "strategy_positions")

    def __init__(self, db: Any, trade_date: Optional[str] = None) -> None:
        self._db = db
        self._trade_date = trade_date or _today_ist()
        self._session: Optional[MonitorSession] = None
        self._candle_ts_sorted: List[int] = []

    def get_session(self) -> MonitorSession:
        if self._session is None:
            self._session = _build_session(
                self._db, self._trade_date,
                self.COLL_SNAPSHOTS, self.COLL_VOTES, self.COLL_SIGNALS, self.COLL_POSITIONS,
            )
            self._candle_ts_sorted = [c.t for c in self._session.candles]
        return self._session

    def get_latest_tick(self) -> Tuple[int, float]:
        """Lightweight query: returns (current_candle_idx, live_price)."""
        session = self.get_session()
        fallback_idx = len(session.candles) - 1
        fallback_price = session.candles[fallback_idx].c
        try:
            doc = self._db[self.COLL_SNAPSHOTS].find_one(
                {"trade_date_ist": self._trade_date},
                {
                    "_id": 0,
                    "timestamp": 1,
                    "payload.snapshot.futures_bar.fut_close": 1,
                },
                sort=[("timestamp", -1)],
            )
            if doc is None:
                return fallback_idx, fallback_price
            ts = _ts_ms(doc.get("timestamp"))
            if ts is None:
                return fallback_idx, fallback_price
            payload = doc.get("payload") or {}
            snapshot = (payload.get("snapshot") or {}) if isinstance(payload, dict) else {}
            futures_bar = (snapshot.get("futures_bar") or {}) if isinstance(snapshot, dict) else {}
            price = _safe_float(futures_bar.get("fut_close"), fallback=None)
            if price is None:
                return fallback_idx, fallback_price
            idx = _nearest_candle_idx(self._candle_ts_sorted, ts)
            return idx, float(price)
        except Exception:
            return fallback_idx, fallback_price


def latest_available_date(db: Any, coll_snapshots: str) -> str:
    """Return the most recent trade_date_ist that has snapshot data."""
    doc = db[coll_snapshots].find_one(
        {"trade_date_ist": {"$exists": True}},
        {"_id": 0, "trade_date_ist": 1},
        sort=[("trade_date_ist", -1)],
    )
    if doc and doc.get("trade_date_ist"):
        return str(doc["trade_date_ist"])
    raise ValueError(f"No trade dates found in {coll_snapshots}")


def latest_replay_date(
    db: Any,
    coll_snapshots: str,
    coll_votes: str,
    coll_positions: str,
) -> str:
    """Prefer a date with completed trades, then visible signals, else latest snapshot date."""
    latest_close = db[coll_positions].find_one(
        {"trade_date_ist": {"$exists": True}, "event": "POSITION_CLOSE"},
        {"_id": 0, "trade_date_ist": 1},
        sort=[("trade_date_ist", -1)],
    )
    if latest_close and latest_close.get("trade_date_ist"):
        return str(latest_close["trade_date_ist"])

    latest_signal = db[coll_votes].find_one(
        {
            "trade_date_ist": {"$exists": True},
            "direction": {"$in": ["PE", "CE", "LONG", "SHORT"]},
        },
        {"_id": 0, "trade_date_ist": 1},
        sort=[("trade_date_ist", -1)],
    )
    if latest_signal and latest_signal.get("trade_date_ist"):
        return str(latest_signal["trade_date_ist"])

    return latest_available_date(db, coll_snapshots)


# ── MongoDB connection ─────────────────────────────────────────────────────────

def make_mongo_db(
    *,
    uri: Optional[str] = None,
    db_name: Optional[str] = None,
) -> Any:
    if uri:
        mongo_uri = uri
    elif os.getenv("MONGO_URI"):
        mongo_uri = os.environ["MONGO_URI"]
    elif os.getenv("MONGODB_URI"):
        mongo_uri = os.environ["MONGODB_URI"]
    else:
        host = os.getenv("MONGO_HOST", "localhost")
        port = os.getenv("MONGO_PORT", "27017")
        mongo_uri = f"mongodb://{host}:{port}"
    db = db_name or os.getenv("MONGO_DB") or os.getenv("MONGODB_DB") or "trading_ai"
    client: MongoClient = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    return client[db]
