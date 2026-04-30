from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

try:
    from .schemas.monitor import (
        MonitorKpiItem,
        MonitorSession,
        MonitorSnapshot,
    )
    from .real_source import LiveMongoSource, MongoSource, latest_replay_date, make_mongo_db
except ImportError:
    from schemas.monitor import (  # type: ignore
        MonitorKpiItem,
        MonitorSession,
        MonitorSnapshot,
    )
    from real_source import LiveMongoSource, MongoSource, latest_replay_date, make_mongo_db  # type: ignore

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_iso_ist() -> str:
    return datetime.now(tz=_IST).isoformat()


def _make_db() -> Any:
    try:
        return make_mongo_db()
    except Exception as exc:
        raise RuntimeError(f"MongoDB unavailable: {exc}") from exc


def _resolve_date(db: Any, requested: Optional[str]) -> str:
    if requested:
        return requested
    return latest_replay_date(
        db,
        MongoSource.COLL_SNAPSHOTS,
        MongoSource.COLL_VOTES,
        MongoSource.COLL_POSITIONS,
    )


# ── Session states ─────────────────────────────────────────────────────────────

class _LiveSessionState:
    def __init__(self, source: LiveMongoSource) -> None:
        self.source = source
        self.session = source.get_session()
        self.current_idx, self.last_price = source.get_latest_tick()
        self._alive = True

    def tick(self) -> None:
        self.current_idx, self.last_price = self.source.get_latest_tick()

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False


class _ReplaySessionState:
    def __init__(self, source: MongoSource, up_to_idx: Optional[int] = None) -> None:
        self.source = source
        self.session = source.get_session()
        self.up_to_idx = int(len(self.session.candles) * 0.55) if up_to_idx is None else up_to_idx
        self.is_playing = False
        self.speed = 4
        self._alive = True

    def step(self) -> None:
        if self.is_playing and self.up_to_idx < len(self.session.candles) - 1:
            self.up_to_idx = min(len(self.session.candles) - 1, self.up_to_idx + self.speed)

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False


# ── KPI builders ───────────────────────────────────────────────────────────────

def _build_kpi_live(state: _LiveSessionState) -> List[MonitorKpiItem]:
    session = state.session
    visible = [t for t in session.trades if t.exitIdx <= state.current_idx]
    total_pnl = sum(t.pnlPct for t in visible)
    wins = sum(1 for t in visible if t.pnlPct > 0)
    wr = (wins / len(visible) * 100) if visible else 0.0
    return [
        MonitorKpiItem(label="ENGINE", value="ML_PURE_V3", sub="stage-1 · stage-2 · policy"),
        MonitorKpiItem(label="INSTRUMENT", value=session.instrument, cls="pos", sub="live · BANKNIFTY"),
        MonitorKpiItem(
            label="SESSION P&L",
            value=f"{total_pnl:+.2f}%",
            cls="pos" if total_pnl >= 0 else "neg",
            sub=f"{len(visible)} trades · {wr:.0f}% WR",
        ),
        MonitorKpiItem(label="PRICE", value=f"{state.last_price:.2f}", sub="futures last"),
        MonitorKpiItem(
            label="CLOCK",
            value=datetime.now(tz=_IST).strftime("%H:%M:%S"),
            sub="IST · Asia/Kolkata",
        ),
        MonitorKpiItem(
            label="BAR",
            value=str(state.current_idx + 1),
            sub=f"of {len(session.candles)}",
        ),
    ]


def _build_kpi_replay(state: _ReplaySessionState) -> List[MonitorKpiItem]:
    session = state.session
    visible = [t for t in session.trades if t.exitIdx <= state.up_to_idx]
    total_pnl = sum(t.pnlPct for t in visible)
    wins = sum(1 for t in visible if t.pnlPct > 0)
    wr = (wins / len(visible) * 100) if visible else 0.0
    pct = (state.up_to_idx + 1) / len(session.candles)
    vt_label = session.candles[state.up_to_idx].label if state.up_to_idx < len(session.candles) else "09:15"
    return [
        MonitorKpiItem(label="VIRTUAL TIME", value=vt_label, sub=session.date),
        MonitorKpiItem(
            label="REPLAY",
            value="RUNNING" if state.is_playing else "PAUSED",
            cls="pos" if state.is_playing else "warn",
            sub=f"{state.speed}× speed",
        ),
        MonitorKpiItem(
            label="SESSION P&L",
            value=f"{total_pnl:+.2f}%",
            cls="pos" if total_pnl >= 0 else "neg",
            sub=f"{len(visible)} trades · {wr:.0f}% WR",
        ),
        MonitorKpiItem(
            label="SIGNALS",
            value=str(len([s for s in session.signals if s.idx <= state.up_to_idx])),
            sub=f"{len([s for s in session.signals if s.idx <= state.up_to_idx and s.fired])} fired",
        ),
        MonitorKpiItem(
            label="PROGRESS",
            value=f"{pct * 100:.0f}%",
            sub=f"{state.up_to_idx + 1}/{len(session.candles)} bars",
        ),
        MonitorKpiItem(label="INSTRUMENT", value=session.instrument, sub=session.date),
    ]


# ── Router ─────────────────────────────────────────────────────────────────────

class DashboardMonitorRouter:
    """Router for the Strategy Monitor SPA."""

    def __init__(self) -> None:
        router = APIRouter(tags=["monitor"])
        router.add_api_route("/api/v1/monitor/snapshot", self.snapshot, methods=["GET"])
        router.add_api_websocket_route("/ws/v1/monitor", self.websocket_monitor)
        self.router = router

    async def snapshot(
        self,
        mode: str = Query("live", description="live or replay"),
        date: Optional[str] = Query(None, description="Replay date YYYY-MM-DD"),
        up_to_idx: Optional[int] = Query(None, description="Replay bar index"),
    ) -> JSONResponse:
        db = _make_db()
        if mode == "live":
            source = LiveMongoSource(db=db, trade_date=date)
            state: Any = _LiveSessionState(source)
            kpi = _build_kpi_live(state)
            snap = MonitorSnapshot(
                mode="live",
                session=state.session,
                up_to_idx=state.current_idx,
                live_idx=state.current_idx,
                live_price=round(state.last_price, 2),
                kpi_items=kpi,
                timestamp=_now_iso_ist(),
            )
        else:
            source = MongoSource(db=db, trade_date=_resolve_date(db, date))
            state = _ReplaySessionState(source, up_to_idx=up_to_idx)
            kpi = _build_kpi_replay(state)
            snap = MonitorSnapshot(
                mode="replay",
                session=state.session,
                up_to_idx=state.up_to_idx,
                live_idx=None,
                live_price=None,
                kpi_items=kpi,
                timestamp=_now_iso_ist(),
            )
        return JSONResponse(content=snap.model_dump(mode="json"))

    async def websocket_monitor(self, ws: WebSocket) -> None:
        await ws.accept()
        state: Optional[Any] = None
        task: Optional[asyncio.Task] = None

        async def _loop() -> None:
            if state is None:
                return
            try:
                while state.alive:
                    if isinstance(state, _LiveSessionState):
                        state.tick()
                        await ws.send_json({
                            "type": "tick",
                            "mode": "live",
                            "up_to_idx": state.current_idx,
                            "live_idx": state.current_idx,
                            "live_price": round(state.last_price, 2),
                            "timestamp": _now_iso_ist(),
                        })
                        await asyncio.sleep(1.0)
                    elif isinstance(state, _ReplaySessionState):
                        state.step()
                        await ws.send_json({
                            "type": "tick",
                            "mode": "replay",
                            "up_to_idx": state.up_to_idx,
                            "timestamp": _now_iso_ist(),
                        })
                        await asyncio.sleep(0.2)
            except Exception as exc:
                logger.debug("Monitor WS loop ended: %s", exc)
            finally:
                if state is not None:
                    state.stop()

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                action = str(msg.get("action") or "").strip().lower()

                if action == "subscribe":
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    mode = str(msg.get("mode") or "live").strip().lower()
                    date_str = str(msg.get("date") or "").strip() or None

                    try:
                        db = _make_db()
                        if mode == "live":
                            src = LiveMongoSource(db=db, trade_date=date_str)
                            state = _LiveSessionState(src)
                            kpi = _build_kpi_live(state)
                        else:
                            src = MongoSource(db=db, trade_date=_resolve_date(db, date_str))
                            up_to = msg.get("up_to_idx")
                            state = _ReplaySessionState(src, up_to_idx=int(up_to) if up_to is not None else None)
                            state.is_playing = bool(msg.get("playing", False))
                            state.speed = max(1, int(msg.get("speed", 4)))
                            kpi = _build_kpi_replay(state)
                    except Exception as exc:
                        await ws.send_json({"type": "error", "message": str(exc)})
                        continue

                    snap = MonitorSnapshot(
                        mode=mode,
                        session=state.session,
                        up_to_idx=state.current_idx if isinstance(state, _LiveSessionState) else state.up_to_idx,
                        live_idx=state.current_idx if isinstance(state, _LiveSessionState) else None,
                        live_price=round(state.last_price, 2) if isinstance(state, _LiveSessionState) else None,
                        kpi_items=kpi,
                        timestamp=_now_iso_ist(),
                    )
                    await ws.send_json({"type": "snapshot", **snap.model_dump(mode="json")})
                    task = asyncio.create_task(_loop())
                    continue

                if action == "control" and isinstance(state, _ReplaySessionState):
                    if "play" in msg:
                        state.is_playing = bool(msg["play"])
                    if "speed" in msg:
                        state.speed = max(1, int(msg["speed"]))
                    if "seek" in msg:
                        state.up_to_idx = max(0, min(len(state.session.candles) - 1, int(msg["seek"])))
                    await ws.send_json({
                        "type": "state",
                        "mode": "replay",
                        "up_to_idx": state.up_to_idx,
                        "is_playing": state.is_playing,
                        "speed": state.speed,
                        "timestamp": _now_iso_ist(),
                    })
                    continue

                if action == "ping":
                    await ws.send_json({"type": "pong", "timestamp": _now_iso_ist()})
                    continue

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("Monitor WS error: %s", exc)
        finally:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if state is not None:
                state.stop()
