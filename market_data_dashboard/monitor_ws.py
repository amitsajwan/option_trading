from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .schemas.monitor import (
    MonitorCandle,
    MonitorKpiItem,
    MonitorSession,
    MonitorSnapshot,
    MonitorTrade,
)
from .monitor_source import MockSource

logger = logging.getLogger(__name__)


class _LiveSessionState:
    """Server-side state for an active live-monitor WebSocket session."""

    def __init__(self, source: MockSource) -> None:
        self.source = source
        self.session = source.get_session()
        self.current_idx = int(len(self.session.candles) * 0.72)
        self.last_price = self.session.candles[self.current_idx].c
        self._alive = True
        self._tick_count = 0

    def tick(self) -> Dict[str, Any]:
        self._tick_count += 1
        if self._tick_count % 27 == 0:
            self.advance_minute()
        candle = self.session.candles[self.current_idx]
        drift = (random.random() - 0.48) * 3
        self.last_price += drift
        candle.c = round(self.last_price, 2)
        if self.last_price > candle.h:
            candle.h = round(self.last_price, 2)
        if self.last_price < candle.l:
            candle.l = round(self.last_price, 2)
        return {
            "idx": self.current_idx,
            "price": round(self.last_price, 2),
        }

    def advance_minute(self) -> None:
        if self.current_idx < len(self.session.candles) - 1:
            self.current_idx += 1
            self.last_price = self.session.candles[self.current_idx].o

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False


class _ReplaySessionState:
    """Server-side state for an active replay-monitor WebSocket session."""

    def __init__(self, source: MockSource, up_to_idx: Optional[int] = None) -> None:
        self.source = source
        self.session = source.get_session()
        self.up_to_idx = int(len(self.session.candles) * 0.55) if up_to_idx is None else up_to_idx
        self.is_playing = False
        self.speed = 4
        self._alive = True

    def step(self) -> Dict[str, Any]:
        if self.is_playing and self.up_to_idx < len(self.session.candles) - 1:
            self.up_to_idx = min(len(self.session.candles) - 1, self.up_to_idx + self.speed)
        return {"up_to_idx": self.up_to_idx}

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False


def _build_kpi_live(state: _LiveSessionState) -> List[MonitorKpiItem]:
    session = state.session
    visible = [t for t in session.trades if t.exitIdx <= state.current_idx]
    total_pnl = sum(t.pnlPct for t in visible)
    wins = sum(1 for t in visible if t.pnlPct > 0)
    wr = (wins / len(visible) * 100) if visible else 0
    return [
        MonitorKpiItem(label="ENGINE", value="ML_PURE_V3", sub="stage-1 · stage-2 · policy", cls=""),
        MonitorKpiItem(label="MARKET", value="OPEN", cls="pos", sub="regime · TREND_UP"),
        MonitorKpiItem(
            label="SESSION P&L",
            value=f"{total_pnl:+.2f}%",
            cls="pos" if total_pnl >= 0 else "neg",
            sub=f"{len(visible)} trades · {wr:.0f}% WR",
        ),
        MonitorKpiItem(label="OPEN", value="0", sub="positions"),
        MonitorKpiItem(label="DATA", value="120ms", cls="pos", sub="fut · opt · vol"),
        MonitorKpiItem(
            label="CLOCK",
            value=datetime.now(
                tz=timezone(timedelta(hours=5, minutes=30))
            ).strftime("%H:%M:%S IST"),
            sub="IST · Asia/Kolkata",
        ),
    ]


def _build_kpi_replay(state: _ReplaySessionState) -> List[MonitorKpiItem]:
    session = state.session
    visible = [t for t in session.trades if t.exitIdx <= state.up_to_idx]
    total_pnl = sum(t.pnlPct for t in visible)
    wins = sum(1 for t in visible if t.pnlPct > 0)
    wr = (wins / len(visible) * 100) if visible else 0
    pct = (state.up_to_idx + 1) / len(session.candles)
    vt_label = session.candles[state.up_to_idx].label if state.up_to_idx < len(session.candles) else "09:15"
    return [
        MonitorKpiItem(label="VIRTUAL TIME", value=vt_label, sub=session.date, cls=""),
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
        MonitorKpiItem(label="ENGINE", value="ML_PURE_V3", sub="run r-2026-0416-ml3"),
    ]


def _now_iso_ist() -> str:
    return datetime.now(tz=timezone(timedelta(hours=5, minutes=30))).isoformat()


class DashboardMonitorRouter:
    """Router for the redesigned Strategy Monitor SPA."""

    def __init__(self) -> None:
        router = APIRouter(tags=["monitor"])
        router.add_api_route("/api/v1/monitor/snapshot", self.snapshot, methods=["GET"])
        router.add_api_websocket_route("/ws/v1/monitor", self.websocket_monitor)
        self.router = router

    async def snapshot(
        self,
        mode: str = Query("live", description="live or replay"),
        date: Optional[str] = Query(None, description="Replay date YYYY-MM-DD"),
        up_to_idx: Optional[int] = Query(None, description="Replay position"),
    ) -> JSONResponse:
        seed = 42 if mode == "live" else 7
        source = MockSource(seed=seed, date=date or "2026-04-16")
        session = source.get_session()

        if mode == "live":
            state = _LiveSessionState(source)
            live_idx = state.current_idx
            live_price = state.last_price
            kpi = _build_kpi_live(state)
            up_idx = state.current_idx
        else:
            state = _ReplaySessionState(source, up_to_idx=up_to_idx)
            live_idx = None
            live_price = None
            kpi = _build_kpi_replay(state)
            up_idx = state.up_to_idx

        snap = MonitorSnapshot(
            mode=mode,
            session=session,
            up_to_idx=up_idx,
            live_idx=live_idx,
            live_price=live_price,
            kpi_items=kpi,
            timestamp=_now_iso_ist(),
        )
        return JSONResponse(content=snap.model_dump(mode="json"))

    async def websocket_monitor(self, ws: WebSocket) -> None:
        await ws.accept()
        state: Optional[Any] = None
        task: Optional[asyncio.Task] = None

        async def _loop() -> None:
            nonlocal state
            if state is None:
                return
            try:
                while state.alive:
                    if isinstance(state, _LiveSessionState):
                        state.tick()
                        # Also advance minute every 6s of wall time (6 iterations at 1s)
                        # We use a simple counter inside the state or just send ticks fast
                        await ws.send_json(
                            {
                                "type": "tick",
                                "mode": "live",
                                "up_to_idx": state.current_idx,
                                "live_idx": state.current_idx,
                                "live_price": round(state.last_price, 2),
                                "timestamp": _now_iso_ist(),
                            }
                        )
                        await asyncio.sleep(0.22)
                    elif isinstance(state, _ReplaySessionState):
                        state.step()
                        await ws.send_json(
                            {
                                "type": "tick",
                                "mode": "replay",
                                "up_to_idx": state.up_to_idx,
                                "timestamp": _now_iso_ist(),
                            }
                        )
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
                    # Cancel prior task
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    mode = str(msg.get("mode") or "live").strip().lower()
                    date = str(msg.get("date") or "").strip() or None
                    seed = 42 if mode == "live" else 7
                    source = MockSource(seed=seed, date=date or "2026-04-16")

                    if mode == "live":
                        state = _LiveSessionState(source)
                        kpi = _build_kpi_live(state)
                    else:
                        up_to = msg.get("up_to_idx")
                        up_to_int = int(up_to) if up_to is not None else None
                        state = _ReplaySessionState(source, up_to_idx=up_to_int)
                        state.is_playing = bool(msg.get("playing", False))
                        state.speed = max(1, int(msg.get("speed", 4)))
                        kpi = _build_kpi_replay(state)

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
                    await ws.send_json(
                        {
                            "type": "state",
                            "mode": "replay",
                            "up_to_idx": state.up_to_idx,
                            "is_playing": state.is_playing,
                            "speed": state.speed,
                            "timestamp": _now_iso_ist(),
                        }
                    )
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
