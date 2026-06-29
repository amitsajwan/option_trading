"""SellerRunner — the live loop (paper or real). Drives the validated pipeline on the
live snapshot feed: manage open spreads INTRADAY, enter once/day in the window.

PAPER mode (default, EXECUTION/mode='paper'): fills come from the chain via PaperLegGateway,
no broker orders — this runs the T9 live paper cycle. REAL mode wires a DhanLegGateway.

Restart-safe (PositionManager durable store). Logs every paper/real action to a JSONL
trade log so the cycle is auditable. NO real money unless gateway is DhanLegGateway AND
operator explicitly enables it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Callable, Optional

from ..market.snapshot_accessor import SnapshotAccessor
from .brain import SellerBrain
from .chain_utils import build_price_fn  # chain-shift-aware price lookup (permanent fix)
from .executor import SafeExecutor, OpenSpread
from .gateway import LegGateway, PaperLegGateway
from .manager import PositionManager, RiskGates

logger = logging.getLogger(__name__)


class SellerRunner:
    def __init__(self, mongo_db, gateway_factory: Optional[Callable[[Callable], LegGateway]] = None,
                 live_collection: str = "phase1_market_snapshots"):
        self._db = mongo_db
        self._col = mongo_db[live_collection]
        self._brain = SellerBrain()
        self._mgr = PositionManager()
        self._risk = RiskGates()
        from ..constants import resolve_lot_size
        self._lot = resolve_lot_size(primary_default=int(os.getenv("BANKNIFTY_LOT_SIZE", "30") or 30))
        self._width = int(os.getenv("SELLER_SPREAD_WIDTH", "300") or 300)
        # default gateway = PAPER (no real money)
        self._gw_factory = gateway_factory or (lambda pf: PaperLegGateway(pf, float(os.getenv("SELLER_SLIPPAGE_PTS", "1.0") or 1.0)))
        self._win = (os.getenv("SELLER_ENTRY_WINDOW", "10:00-14:00") or "10:00-14:00").split("-")
        self._log_path = os.path.join(os.getenv("STRATEGY_RUN_DIR", "/tmp"), "seller_trades.jsonl")
        self._daily_pnl = 0.0
        self._cur_day: Optional[str] = None
        self._entered_today = False
        self._mode = "live" if (os.getenv("EXECUTION_ADAPTER", "paper").strip().lower() == "dhan"
                                and (os.getenv("SELLER_LIVE_ENABLED", "0") or "0").strip() in ("1", "true", "yes")) else "paper"
        logger.info("SellerRunner up (lot=%d width=%d mode=%s open=%d)", self._lot, self._width,
                    self._mode, len(self._mgr.open_spreads))

    # ── mongo mirror (for the dashboard) ─────────────────────────────────────
    def _publish_status(self, snap, acc, decision) -> None:
        try:
            self._db["seller_status"].update_one({"_id": "live"}, {"$set": {
                "_id": "live", "ts": datetime.now(timezone.utc).isoformat(), "time": self._hhmm(snap),
                "mode": self._mode, "decision": decision.structure if decision.fires else "SIT OUT",
                "reason": (decision.reason or "")[:80], "iv_rank": round(acc.iv_percentile or 0, 1),
                "fires": bool(decision.fires), "open_count": len(self._mgr.open_spreads),
            }}, upsert=True)
        except Exception:
            pass

    def _mirror_open(self, spread, decision) -> None:
        try:
            self._db["seller_positions"].insert_one({
                "spread_id": spread.spread_id, "day": spread.trade_date, "structure": spread.structure,
                "credit": spread.entry_credit, "iv_rank": decision.iv_rank, "opened_at": spread.opened_at,
                "legs": [[l.action, l.option_type, l.strike] for l in spread.legs]})
        except Exception:
            pass

    def _mirror_close(self, spread, reason, held, pnl) -> None:
        try:
            self._db["seller_trades"].insert_one({
                "source": "live", "spread_id": spread.spread_id, "day": spread.trade_date,
                "structure": spread.structure, "credit": spread.entry_credit, "reason": reason,
                "days_held": held, "pnl_rs": round(pnl), "iv_rank": (spread.meta or {}).get("iv_rank"),
                "legs": [[l.action, l.option_type, l.strike] for l in spread.legs],
                "entry_ts": spread.opened_at, "exit_ts": datetime.now(timezone.utc).isoformat()})
            self._db["seller_positions"].delete_one({"spread_id": spread.spread_id})
        except Exception:
            pass

    def _log(self, event: str, **kw):
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
        try:
            with open(self._log_path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        logger.info("seller %s %s", event, kw)

    def _latest(self) -> Optional[dict]:
        doc = self._col.find_one(sort=[("timestamp", -1)])
        return (doc.get("payload") or {}).get("snapshot") if doc else None

    @staticmethod
    def _hhmm(snap: dict) -> str:
        t = ((snap.get("session_context") or {}).get("time") or snap.get("timestamp") or "")
        return t[11:16] if len(t) > 15 else t[:5]

    @staticmethod
    def _trade_date(snap: dict) -> Optional[str]:
        """Real ISO trade date — NEVER fall back to HH:MM (that would reset daily gates every
        minute). Tries trade_date_ist -> session_context.date -> timestamp[:10]. (review H4/C-fix)"""
        sc = snap.get("session_context") or {}
        raw = snap.get("trade_date_ist") or sc.get("date") or str(sc.get("timestamp") or snap.get("timestamp") or "")[:10]
        try:
            return date.fromisoformat(str(raw)[:10]).isoformat()
        except Exception:
            return None

    def _expiry(self, snap: dict) -> date:
        # TODO: resolve the real nearest BankNifty monthly expiry from the chain/scrip master.
        raw = ((snap.get("session_context") or {}).get("expiry") or snap.get("expiry"))
        try:
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            return date(2099, 1, 1)  # paper placeholder

    def on_snapshot(self, snap: dict) -> None:
        if not snap or not snap.get("strikes"):
            return
        day = self._trade_date(snap)
        if day and day != self._cur_day:   # only reset daily gates on a REAL date rollover
            self._cur_day, self._daily_pnl, self._entered_today = day, 0.0, False
        pf = build_price_fn(snap)
        expiry = self._expiry(snap)
        acc = SnapshotAccessor(snap)
        decision = self._brain.decide(acc)
        self._publish_status(snap, acc, decision)
        # ── manage open spreads INTRADAY ──
        for sp in list(self._mgr.open_spreads):
            val = PositionManager.spread_value(sp, pf)
            if val is None:
                continue
            held = 0  # days held, from the SNAPSHOT date (not wall-clock — replay-safe). review H1
            try:
                held = (date.fromisoformat(day or self._cur_day) - date.fromisoformat(sp.trade_date)).days
            except Exception:
                held = 0
            reason = self._mgr.check_exit(sp, val, held, dte=acc.days_to_expiry)
            if reason:
                ex = SafeExecutor(self._gw_factory(pf), sp.qty, self._width)
                exit_val = ex.close_spread(sp, expiry)
                if exit_val is None:
                    # A leg failed to square off. KEEP the spread in the durable store and retry
                    # next tick — NEVER drop a still-live position from tracking. (review C1)
                    self._log("close_failed", spread_id=sp.spread_id, reason=reason)
                    continue
                pnl = (sp.entry_credit - exit_val) * sp.qty
                self._daily_pnl += pnl
                self._log("close", spread_id=sp.spread_id, structure=sp.structure, reason=reason,
                          credit=sp.entry_credit, exit_value=exit_val, pnl_rs=round(pnl, 1))
                self._mirror_close(sp, reason, held, pnl)
                self._mgr.remove(sp.spread_id)
        # ── entry: once/day, in window, risk-permitting ──
        if self._entered_today:
            return
        hh = self._hhmm(snap)
        if not (self._win[0] <= hh <= self._win[1]):
            return
        ok, why = self._risk.can_open(len(self._mgr.open_spreads), self._daily_pnl)
        if not ok:
            return
        if not decision.fires:
            return
        ex = SafeExecutor(self._gw_factory(pf), self._lot, self._width)
        spread = ex.open_spread(decision, expiry, trade_date=str(day))
        if spread is not None:
            self._mgr.add(spread)
            self._entered_today = True
            self._log("open", spread_id=spread.spread_id, structure=spread.structure,
                      credit=spread.entry_credit, legs=[(l.action, l.option_type, l.strike) for l in spread.legs],
                      regime=decision.regime, iv_rank=decision.iv_rank)
            self._mirror_open(spread, decision)

    def run_forever(self, interval_s: float = 30.0) -> None:
        logger.info("SellerRunner loop start (interval=%.0fs, log=%s)", interval_s, self._log_path)
        while True:
            try:
                snap = self._latest()
                if snap:
                    self.on_snapshot(snap)
            except Exception:
                logger.exception("seller loop error")
            time.sleep(interval_s)
