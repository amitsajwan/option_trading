from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class MonitorSignalMetrics(BaseModel):
    entry_prob: float = Field(..., ge=0.0, le=1.0)
    trade_prob: float = Field(..., ge=0.0, le=1.0)
    up_prob: float = Field(..., ge=0.0, le=1.0)
    ce_prob: float = Field(..., ge=0.0, le=1.0)
    pe_prob: float = Field(..., ge=0.0, le=1.0)
    recipe_prob: float = Field(..., ge=0.0, le=1.0)
    recipe_margin: float = Field(..., ge=0.0, le=1.0)


class MonitorSignal(BaseModel):
    t: int
    idx: int
    strat: str
    dir: str
    conf: float = Field(..., ge=0.0, le=1.0)
    fired: bool
    reason: str
    detail: str = ""
    metrics: MonitorSignalMetrics
    regime: str


class MonitorTrade(BaseModel):
    id: str
    t: int
    tLabel: str
    strat: str
    dir: str
    qty: int
    entry: float
    exit: float
    entryIdx: int
    exitIdx: int
    pnlPct: float
    hold: str
    signal: MonitorSignal
    entryReason: str = ""
    entryDetail: str = ""
    exitReason: str = ""
    exitDetail: str = ""
    stopLossPct: Optional[float] = None
    targetPct: Optional[float] = None
    maxHoldBars: Optional[int] = None
    stopPrice: Optional[float] = None
    stopBasis: Optional[str] = None
    entryFuturesPrice: Optional[float] = None
    underlyingStopPrice: Optional[float] = None
    stopTriggerCandle: Optional[str] = None
    stopTriggerDetail: str = ""


class MonitorAlert(BaseModel):
    level: str
    t: str
    msg: str
    tms: int


class MonitorCandle(BaseModel):
    i: int
    o: float
    h: float
    l: float
    c: float
    v: int
    t: int
    label: str


class MonitorSession(BaseModel):
    date: str
    instrument: str
    candles: List[MonitorCandle]
    signals: List[MonitorSignal]
    trades: List[MonitorTrade]
    alerts: List[MonitorAlert]
    basePrice: float
    runId: Optional[str] = None


class MonitorKpiItem(BaseModel):
    label: str
    value: str
    sub: str = ""
    cls: str = ""


class MonitorSnapshot(BaseModel):
    mode: str
    session: MonitorSession
    up_to_idx: int
    live_idx: Optional[int] = None
    live_price: Optional[float] = None
    kpi_items: List[MonitorKpiItem]
    timestamp: str


class MonitorTick(BaseModel):
    type: str = "tick"
    mode: str
    up_to_idx: int
    live_idx: Optional[int] = None
    live_price: Optional[float] = None
    timestamp: str
