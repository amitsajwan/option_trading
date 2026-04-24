from __future__ import annotations

import math
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

from .schemas.monitor import (
    MonitorAlert,
    MonitorCandle,
    MonitorSession,
    MonitorSignal,
    MonitorSignalMetrics,
    MonitorTrade,
)


def _fmt_time(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone(timedelta(hours=5, minutes=30)))
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _fmt_hold(entry_ms: int, exit_ms: int) -> str:
    s = max(0, int((exit_ms - entry_ms) / 1000))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


class MockSource:
    """Deterministic mock data generator mirroring core.js TradingCore."""

    STRATEGIES = ["ml_pure_v3", "mom_breakout", "mean_rev_5m", "gap_fade", "vol_compress"]
    REASON_CODES = ["ENTRY_MET", "EDGE_THRESH", "CONF_LOW", "REGIME_BLOCK", "WARMUP", "COOLDOWN"]
    REGIMES = ["TREND_UP", "CHOP", "TREND_DOWN"]

    def __init__(self, seed: int = 42, base_price: float = 22480.0, date: str = "2026-04-24") -> None:
        self._seed = seed
        self._base_price = base_price
        self._date = date
        self._rng = random.Random(seed)
        self._session: Optional[MonitorSession] = None

    def _next_f(self) -> float:
        return self._rng.random()

    def build_candles(self) -> List[MonitorCandle]:
        self._rng = random.Random(self._seed)
        candles: List[MonitorCandle] = []
        price = self._base_price
        # 09:15 IST start
        day_start = datetime(
            2026, 4, 24, 9, 15, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
        trend = (self._next_f() - 0.5) * 0.002
        for i in range(375):
            vol = 6 + self._next_f() * 14
            drift = (self._next_f() - 0.5) * 12 + trend * price
            open_p = price
            close_p = open_p + drift
            t = i / 375.0
            wave = math.sin(t * math.pi * 2) * 0.0008 * price
            close_p += wave * (self._next_f() - 0.5) * 2
            high_p = max(open_p, close_p) + self._next_f() * vol
            low_p = min(open_p, close_p) - self._next_f() * vol
            ts = int((day_start.timestamp() + i * 60) * 1000)
            candles.append(
                MonitorCandle(
                    i=i,
                    o=round(open_p, 2),
                    h=round(high_p, 2),
                    l=round(low_p, 2),
                    c=round(close_p, 2),
                    v=int(40000 + self._next_f() * 80000),
                    t=ts,
                    label=_fmt_time(ts),
                )
            )
            price = close_p
        return candles

    def build_signals(self, candles: List[MonitorCandle], count: int = 52) -> List[MonitorSignal]:
        rng = random.Random(self._seed + 1)
        out: List[MonitorSignal] = []
        for _ in range(count):
            idx = int(30 + rng.random() * (len(candles) - 40))
            candle = candles[idx]
            direction = "LONG" if rng.random() > 0.5 else "SHORT"
            conf = 0.45 + rng.random() * 0.5
            fired = conf > 0.65 and rng.random() > 0.55
            reason = "ENTRY_MET" if fired else self.REASON_CODES[int(rng.random() * len(self.REASON_CODES))]
            metrics = MonitorSignalMetrics(
                entry_prob=0.3 + rng.random() * 0.6,
                trade_prob=0.4 + rng.random() * 0.5,
                up_prob=(0.5 + rng.random() * 0.4) if direction == "LONG" else (0.2 + rng.random() * 0.3),
                ce_prob=(0.5 + rng.random() * 0.4) if direction == "LONG" else (0.1 + rng.random() * 0.3),
                pe_prob=(0.5 + rng.random() * 0.4) if direction == "SHORT" else (0.1 + rng.random() * 0.3),
                recipe_prob=0.4 + rng.random() * 0.5,
                recipe_margin=rng.random() * 0.3,
            )
            out.append(
                MonitorSignal(
                    t=candle.t,
                    idx=idx,
                    strat=self.STRATEGIES[int(rng.random() * len(self.STRATEGIES))],
                    dir=direction,
                    conf=round(conf, 4),
                    fired=fired,
                    reason=reason,
                    metrics=metrics,
                    regime=self.REGIMES[int(rng.random() * len(self.REGIMES))],
                )
            )
        out.sort(key=lambda s: s.idx)
        return out

    def build_trades(self, signals: List[MonitorSignal], candles: List[MonitorCandle]) -> List[MonitorTrade]:
        rng = random.Random(self._seed + 2)
        out: List[MonitorTrade] = []
        for sig in signals:
            if not sig.fired:
                continue
            entry_candle = candles[sig.idx]
            exit_idx = min(len(candles) - 1, sig.idx + 3 + int(rng.random() * 12))
            exit_candle = candles[exit_idx]
            entry_px = entry_candle.c
            exit_px = exit_candle.c
            pnl_raw = (exit_px - entry_px) if sig.dir == "LONG" else (entry_px - exit_px)
            pnl_pct = (pnl_raw / entry_px) * 100 * (2 + rng.random() * 3)
            out.append(
                MonitorTrade(
                    id=f"T{sig.idx}",
                    t=sig.t,
                    tLabel=_fmt_time(sig.t),
                    strat=sig.strat,
                    dir=sig.dir,
                    qty=50 + int(rng.random() * 200),
                    entry=round(entry_px, 2),
                    exit=round(exit_px, 2),
                    entryIdx=sig.idx,
                    exitIdx=exit_idx,
                    pnlPct=round(pnl_pct, 2),
                    hold=_fmt_hold(entry_candle.t, exit_candle.t),
                    signal=sig,
                )
            )
        return out

    def build_alerts(self) -> List[MonitorAlert]:
        now = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        return [
            MonitorAlert(level="info", t="09:16", msg="<strong>ml_pure_v3</strong> engine armed — stage-1 threshold 0.62", tms=now - 3600000),
            MonitorAlert(level="warn", t="10:42", msg="<strong>Regime drift</strong> detected — recipe margin below baseline", tms=now - 1800000),
            MonitorAlert(level="info", t="12:05", msg="<strong>Lunchtime lull</strong> — entry rate throttled automatically", tms=now - 900000),
            MonitorAlert(level="crit", t="13:21", msg="<strong>Data freshness</strong> exceeded 5s on options feed", tms=now - 600000),
            MonitorAlert(level="info", t="14:10", msg="<strong>Cooldown</strong> active after 3 consecutive losses", tms=now - 300000),
        ]

    def get_session(self) -> MonitorSession:
        if self._session is not None:
            return self._session
        candles = self.build_candles()
        signals = self.build_signals(candles)
        trades = self.build_trades(signals, candles)
        alerts = self.build_alerts()
        self._session = MonitorSession(
            date=self._date,
            instrument="NIFTY 50",
            candles=candles,
            signals=signals,
            trades=trades,
            alerts=alerts,
            basePrice=candles[0].c,
        )
        return self._session

    @staticmethod
    def strategy_contribution(trades: List[MonitorTrade]) -> List[Dict[str, Any]]:
        cmap: Dict[str, Dict[str, Any]] = {}
        for tr in trades:
            if tr.strat not in cmap:
                cmap[tr.strat] = {"label": tr.strat, "value": 0.0, "n": 0}
            cmap[tr.strat]["value"] += tr.pnlPct
            cmap[tr.strat]["n"] += 1
        rows = sorted(cmap.values(), key=lambda r: r["value"], reverse=True)
        return rows
