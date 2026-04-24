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


def _safe_float(v: Any, fallback: Optional[float] = 0.0) -> Optional[float]:
    try:
        out = float(v)
        if math.isnan(out) or math.isinf(out):
            return fallback
        return out
    except Exception:
        return fallback


def _ts_ms(value: Any) -> Optional[int]:
    """Return epoch-milliseconds from an ISO string or datetime."""
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
    """Return the index of the candle whose timestamp is closest to ts_ms."""
    if not candle_ts_sorted:
        return 0
    pos = bisect.bisect_right(candle_ts_sorted, ts_ms)
    if pos == 0:
        return 0
    if pos == len(candle_ts_sorted):
        return len(candle_ts_sorted) - 1
    before = pos - 1
    after = pos
    if abs(candle_ts_sorted[before] - ts_ms) <= abs(candle_ts_sorted[after] - ts_ms):
        return before
    return after


# ── Transforms ────────────────────────────────────────────────────────────────

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

    session_context = snapshot.get("session_context") or {}
    if not isinstance(session_context, dict):
        session_context = {}

    raw_ts = doc.get("timestamp") or session_context.get("timestamp")
    ts = _ts_ms(raw_ts)
    if ts is None:
        return None

    label = str(session_context.get("time") or "").strip()
    if not label:
        label = _fmt_time(ts)

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


def _vote_to_signal(
    doc: Dict[str, Any],
    candle_ts_sorted: List[int],
) -> Optional[MonitorSignal]:
    payload = doc.get("payload") or {}
    payload_signal = (payload.get("signal") or {}) if isinstance(payload, dict) else {}
    if not isinstance(payload_signal, dict):
        payload_signal = {}

    # Resolve signal_type — only map ENTRY signals
    signal_type = str(
        doc.get("signal_type") or payload_signal.get("signal_type") or ""
    ).strip().upper()
    if signal_type and signal_type != "ENTRY":
        return None

    raw_ts = doc.get("timestamp") or payload_signal.get("timestamp")
    ts = _ts_ms(raw_ts)
    if ts is None:
        return None

    idx = _nearest_candle_idx(candle_ts_sorted, ts)

    direction = str(
        doc.get("direction") or payload_signal.get("direction") or "LONG"
    ).strip().upper()
    if direction not in ("LONG", "SHORT"):
        direction = "LONG"

    conf = _safe_float(
        doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence"),
        fallback=0.5,
    )
    conf = max(0.0, min(1.0, conf))

    regime = str(doc.get("regime") or payload_signal.get("regime") or "UNKNOWN").strip()

    decision_metrics = doc.get("decision_metrics")
    if not isinstance(decision_metrics, dict):
        decision_metrics = payload_signal.get("decision_metrics") or {}
    if not isinstance(decision_metrics, dict):
        decision_metrics = {}

    reason_code = str(
        doc.get("decision_reason_code") or payload_signal.get("decision_reason_code") or "UNKNOWN"
    ).strip()

    fired = reason_code == "ENTRY_MET" or (conf > 0.65 and not reason_code)

    # contributing_strategies → strat label
    strat_list = payload_signal.get("contributing_strategies")
    if isinstance(strat_list, list) and strat_list:
        strat = str(strat_list[0] or "unknown").strip()
    else:
        strat = str(doc.get("strategy") or payload_signal.get("strategy") or "unknown").strip()

    entry_prob = _safe_float(decision_metrics.get("ml_entry_prob"), 0.5)
    trade_prob = _safe_float(decision_metrics.get("ml_entry_prob"), 0.5)
    up_prob = _safe_float(decision_metrics.get("ml_direction_up_prob"), 0.5)
    ce_prob = _safe_float(decision_metrics.get("ml_ce_prob"), 0.5)
    pe_prob = _safe_float(decision_metrics.get("ml_pe_prob"), 0.5)
    recipe_prob = _safe_float(decision_metrics.get("ml_recipe_prob"), 0.5)
    recipe_margin = min(1.0, _safe_float(decision_metrics.get("ml_recipe_margin"), 0.0))

    def _clamp01(v: float) -> float:
        return max(0.0, min(1.0, v))

    metrics = MonitorSignalMetrics(
        entry_prob=_clamp01(entry_prob),
        trade_prob=_clamp01(trade_prob),
        up_prob=_clamp01(up_prob),
        ce_prob=_clamp01(ce_prob),
        pe_prob=_clamp01(pe_prob),
        recipe_prob=_clamp01(recipe_prob),
        recipe_margin=_clamp01(recipe_margin),
    )

    return MonitorSignal(
        t=ts,
        idx=idx,
        strat=strat or "unknown",
        dir=direction,
        conf=round(conf, 4),
        fired=fired,
        reason=reason_code or "UNKNOWN",
        metrics=metrics,
        regime=regime or "UNKNOWN",
    )


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

    entry_px = _safe_float(open_pos.get("entry_premium") or candles[entry_idx].c, fallback=candles[entry_idx].c) or candles[entry_idx].c
    exit_px = _safe_float(close_pos.get("exit_premium") or candles[exit_idx].c, fallback=candles[exit_idx].c) or candles[exit_idx].c

    pnl_pct = _safe_float(close_pos.get("pnl_pct"), fallback=0.0) or 0.0

    direction = str(open_pos.get("direction") or "LONG").strip().upper()
    if direction not in ("LONG", "SHORT"):
        direction = "LONG"

    strat_list = open_pos.get("contributing_strategies")
    if isinstance(strat_list, list) and strat_list:
        strat = str(strat_list[0] or "unknown").strip()
    else:
        strat = str(open_pos.get("entry_strategy") or open_pos.get("strategy") or "unknown").strip()

    if signal is None:
        placeholder_metrics = MonitorSignalMetrics(
            entry_prob=0.5, trade_prob=0.5, up_prob=0.5,
            ce_prob=0.5, pe_prob=0.5, recipe_prob=0.5, recipe_margin=0.0,
        )
        signal = MonitorSignal(
            t=entry_ts, idx=entry_idx, strat=strat, dir=direction,
            conf=0.5, fired=True, reason="ENTRY_MET",
            metrics=placeholder_metrics, regime="UNKNOWN",
        )

    return MonitorTrade(
        id=position_id,
        t=entry_ts,
        tLabel=_fmt_time(entry_ts),
        strat=strat or signal.strat,
        dir=direction,
        qty=int(_safe_float(open_pos.get("lots") or open_pos.get("qty"), fallback=1)),
        entry=round(entry_px, 2),
        exit=round(exit_px, 2),
        entryIdx=entry_idx,
        exitIdx=exit_idx,
        pnlPct=round(pnl_pct, 2),
        hold=_fmt_hold(entry_ts, exit_ts),
        signal=signal,
    )


# ── MongoSource ────────────────────────────────────────────────────────────────

class MongoSource:
    """Session builder backed by MongoDB historical collections."""

    COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
    COLL_VOTES = os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL", "strategy_votes_historical")
    COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL", "strategy_positions_historical")

    def __init__(self, db: Any, trade_date: str) -> None:
        self._db = db
        self._trade_date = trade_date
        self._session: Optional[MonitorSession] = None

    def get_session(self) -> MonitorSession:
        if self._session is not None:
            return self._session
        self._session = self._build()
        return self._session

    def _build(self) -> MonitorSession:
        date_q: Dict[str, Any] = {"trade_date_ist": self._trade_date}

        # ── 1. Candles ──────────────────────────────────────────────────────
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
        raw_snaps = list(
            self._db[self.COLL_SNAPSHOTS]
            .find(date_q, snap_proj)
            .sort("timestamp", ASCENDING)
        )
        candles: List[MonitorCandle] = []
        instrument = "BANKNIFTY-I"
        for i, doc in enumerate(raw_snaps):
            c = _snapshot_to_candle(doc, i)
            if c is not None:
                candles.append(c)
                if instrument == "BANKNIFTY-I":
                    val = str(doc.get("instrument") or "").strip()
                    if val:
                        instrument = val

        if not candles:
            raise ValueError(f"No snapshot candle data found for {self._trade_date}")

        candle_ts_sorted = [c.t for c in candles]

        # ── 2. Signals ──────────────────────────────────────────────────────
        vote_proj = {
            "_id": 0,
            "signal_id": 1,
            "timestamp": 1,
            "signal_type": 1,
            "direction": 1,
            "confidence": 1,
            "regime": 1,
            "strategy": 1,
            "reason": 1,
            "decision_metrics": 1,
            "decision_reason_code": 1,
            "payload.signal": 1,
        }
        signal_by_id: Dict[str, MonitorSignal] = {}
        signals_raw: List[MonitorSignal] = []
        for doc in (
            self._db[self.COLL_VOTES].find(date_q, vote_proj).sort("timestamp", ASCENDING)
        ):
            sig = _vote_to_signal(doc, candle_ts_sorted)
            if sig is None:
                continue
            signals_raw.append(sig)
            sid = str(doc.get("signal_id") or "").strip()
            if sid:
                signal_by_id[sid] = sig

        # ── 3. Positions → Trades ────────────────────────────────────────────
        pos_proj = {
            "_id": 0,
            "position_id": 1,
            "signal_id": 1,
            "event": 1,
            "timestamp": 1,
            "payload.position": 1,
        }
        position_map: Dict[str, Dict[str, Any]] = {}
        for doc in (
            self._db[self.COLL_POSITIONS].find(date_q, pos_proj).sort("timestamp", ASCENDING)
        ):
            pid = str(doc.get("position_id") or "").strip()
            if not pid:
                continue
            payload_pos = (doc.get("payload") or {}).get("position") or {}
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

        trades: List[MonitorTrade] = []
        for pid, docs in position_map.items():
            open_pos = docs.get("open")
            close_pos = docs.get("close")
            if not isinstance(open_pos, dict) or not isinstance(close_pos, dict):
                continue
            sid = docs.get("signal_id", "")
            linked_signal = signal_by_id.get(sid) if sid else None
            trade = _position_to_trade(
                pid,
                open_pos,
                close_pos,
                docs.get("open_doc") or {},
                docs.get("close_doc") or {},
                linked_signal,
                candle_ts_sorted,
                candles,
            )
            if trade is not None:
                trades.append(trade)

        trades.sort(key=lambda t: t.entryIdx)

        # ── 4. Alerts (static placeholder for now) ───────────────────────────
        alerts = _build_default_alerts(self._trade_date)

        return MonitorSession(
            date=self._trade_date,
            instrument=instrument,
            candles=candles,
            signals=signals_raw,
            trades=trades,
            alerts=alerts,
            basePrice=candles[0].c,
        )


def _build_default_alerts(trade_date: str) -> List[MonitorAlert]:
    now = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return [
        MonitorAlert(
            level="info",
            t="09:15",
            msg=f"<strong>Historical replay</strong> — {trade_date}",
            tms=now,
        ),
    ]


def make_mongo_db(
    *,
    uri: Optional[str] = None,
    db_name: Optional[str] = None,
) -> Any:
    """Return a pymongo Database object using env vars as defaults."""
    mongo_uri = uri or os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
    db = db_name or os.getenv("MONGO_DB") or os.getenv("MONGODB_DB") or "market_data"
    client: MongoClient = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    return client[db]
