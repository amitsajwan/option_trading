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


def _vote_to_signal(
    doc: Dict[str, Any],
    candle_ts_sorted: List[int],
) -> Optional[MonitorSignal]:
    payload = doc.get("payload") or {}
    payload_signal = (payload.get("signal") or {}) if isinstance(payload, dict) else {}
    if not isinstance(payload_signal, dict):
        payload_signal = {}

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

    conf = max(0.0, min(1.0, _safe_float(
        doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence"),
        fallback=0.5,
    ) or 0.5))

    regime = str(doc.get("regime") or payload_signal.get("regime") or "UNKNOWN").strip()

    dm = doc.get("decision_metrics")
    if not isinstance(dm, dict):
        dm = payload_signal.get("decision_metrics") or {}
    if not isinstance(dm, dict):
        dm = {}

    reason_code = str(
        doc.get("decision_reason_code") or payload_signal.get("decision_reason_code") or "UNKNOWN"
    ).strip()
    fired = reason_code == "ENTRY_MET" or (conf > 0.65 and not reason_code)

    contrib = payload_signal.get("contributing_strategies")
    if isinstance(contrib, list) and contrib:
        strat = str(contrib[0] or "unknown").strip()
    else:
        strat = str(doc.get("strategy") or payload_signal.get("strategy") or "unknown").strip()

    def _p(key: str, default: float = 0.5) -> float:
        return max(0.0, min(1.0, _safe_float(dm.get(key), default) or default))

    metrics = MonitorSignalMetrics(
        entry_prob=_p("ml_entry_prob"),
        trade_prob=_p("ml_entry_prob"),
        up_prob=_p("ml_direction_up_prob"),
        ce_prob=_p("ml_ce_prob"),
        pe_prob=_p("ml_pe_prob"),
        recipe_prob=_p("ml_recipe_prob"),
        recipe_margin=max(0.0, min(1.0, _safe_float(dm.get("ml_recipe_margin"), 0.0) or 0.0)),
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

    entry_px = _safe_float(open_pos.get("entry_premium"), fallback=None)
    entry_px = float(entry_px) if entry_px is not None else candles[entry_idx].c
    exit_px = _safe_float(close_pos.get("exit_premium"), fallback=None)
    exit_px = float(exit_px) if exit_px is not None else candles[exit_idx].c

    pnl_pct = float(_safe_float(close_pos.get("pnl_pct"), fallback=0.0) or 0.0)

    direction = str(open_pos.get("direction") or "LONG").strip().upper()
    if direction not in ("LONG", "SHORT"):
        direction = "LONG"

    contrib = open_pos.get("contributing_strategies")
    if isinstance(contrib, list) and contrib:
        strat = str(contrib[0] or "unknown").strip()
    else:
        strat = str(open_pos.get("entry_strategy") or open_pos.get("strategy") or "unknown").strip()

    if signal is None:
        signal = MonitorSignal(
            t=entry_ts, idx=entry_idx, strat=strat or "unknown", dir=direction,
            conf=0.5, fired=True, reason="ENTRY_MET",
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
    )


# ── Shared build logic ─────────────────────────────────────────────────────────

def _build_session(
    db: Any,
    trade_date: str,
    coll_snapshots: str,
    coll_votes: str,
    coll_positions: str,
) -> MonitorSession:
    date_q: Dict[str, Any] = {"trade_date_ist": trade_date}

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
    for doc in db[coll_votes].find(date_q, vote_proj).sort("timestamp", ASCENDING):
        sig = _vote_to_signal(doc, candle_ts_sorted)
        if sig is None:
            continue
        signals.append(sig)
        sid = str(doc.get("signal_id") or "").strip()
        if sid:
            signal_by_id[sid] = sig

    pos_proj = {
        "_id": 0,
        "position_id": 1,
        "signal_id": 1,
        "event": 1,
        "timestamp": 1,
        "payload.position": 1,
    }
    position_map: Dict[str, Dict[str, Any]] = {}
    for doc in db[coll_positions].find(date_q, pos_proj).sort("timestamp", ASCENDING):
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

    trades.sort(key=lambda t: t.entryIdx)

    return MonitorSession(
        date=trade_date,
        instrument=instrument,
        candles=candles,
        signals=signals,
        trades=trades,
        alerts=[MonitorAlert(
            level="info", t="09:15",
            msg=f"<strong>Session loaded</strong> — {trade_date}",
            tms=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        )],
        basePrice=candles[0].c,
    )


# ── Sources ────────────────────────────────────────────────────────────────────

class MongoSource:
    """Historical replay — reads from *_historical collections."""

    COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS_HISTORICAL", "phase1_market_snapshots_historical")
    COLL_VOTES = os.getenv("MONGO_COLL_STRATEGY_VOTES_HISTORICAL", "strategy_votes_historical")
    COLL_POSITIONS = os.getenv("MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL", "strategy_positions_historical")

    def __init__(self, db: Any, trade_date: str) -> None:
        self._db = db
        self._trade_date = trade_date
        self._session: Optional[MonitorSession] = None

    def get_session(self) -> MonitorSession:
        if self._session is None:
            self._session = _build_session(
                self._db, self._trade_date,
                self.COLL_SNAPSHOTS, self.COLL_VOTES, self.COLL_POSITIONS,
            )
        return self._session


class LiveMongoSource:
    """Live session — reads from live (non-historical) collections and supports tick queries."""

    COLL_SNAPSHOTS = os.getenv("MONGO_COLL_SNAPSHOTS", "phase1_market_snapshots")
    COLL_VOTES = os.getenv("MONGO_COLL_STRATEGY_VOTES", "strategy_votes")
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
                self.COLL_SNAPSHOTS, self.COLL_VOTES, self.COLL_POSITIONS,
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
