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
from datetime import date, datetime, timedelta, timezone
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
        # Phase 0 (2026-07-10): entry-failure latch. 2026-07-09 a failed open
        # (margin) retried every 30s, buying+unwinding hedge legs each cycle
        # (~₹30-60 burnt per cycle). N failures => stand down for the DAY.
        self._entry_fail_count = 0
        self._entry_fail_latch = int(os.getenv("SELLER_ENTRY_FAIL_LATCH", "2") or 2)
        self._min_free_margin = float(os.getenv("SELLER_MIN_FREE_MARGIN_RS", "60000") or 60000)
        # Credit floor (Phase 1 replay finding, 2026-07-10): the brain sometimes
        # proposes structures whose chain prices net ~zero or NEGATIVE credit; the
        # executor only discovers this AFTER filling legs and unwinds — at real
        # spread cost in live. Gate on the ESTIMATED credit BEFORE leg 1: thin
        # credit is also just a bad sale (coins in front of the steamroller).
        self._min_credit_frac = float(os.getenv("SELLER_MIN_CREDIT_FRAC", "0.30") or 0.30)
        self._reconciled_day: Optional[str] = None
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

    def _alert(self, text: str) -> None:
        """Telegram trade alert — LIVE mode only (paper/replay never pushes)."""
        if self._mode != "live":
            return
        try:
            from execution_app.alerts import _send_telegram
            _send_telegram(text)
        except Exception:
            logger.exception("seller: alert send failed (ignored)")

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

    def _expiry(self, snap: dict) -> Optional[date]:
        """Real expiry for order legs. 2026-07-09 live bug: snapshots carry no
        'expiry' field, so this returned the date(2099,1,1) paper placeholder and
        EVERY real-money leg failed scrip-master resolution — the seller could
        never open a live position. Snapshots DO carry days_to_expiry, so derive
        the date from it; as a last resort return None, which DhanAdapter's
        _resolve_qty resolves to the nearest listed expiry (paper gateway ignores
        expiry entirely)."""
        sc = snap.get("session_context") or {}
        raw = sc.get("expiry") or snap.get("expiry")
        try:
            return date.fromisoformat(str(raw)[:10])
        except Exception:
            pass
        try:
            dte = int(sc.get("days_to_expiry"))
            day = self._trade_date(snap)
            if day is not None and dte >= 0:
                return date.fromisoformat(day) + timedelta(days=dte)
        except Exception:
            pass
        return None

    # ── Phase 0 helpers (2026-07-10) ─────────────────────────────────────────
    def _adapter(self, pf):
        """The DhanAdapter behind the live gateway, or None in paper mode."""
        if self._mode != "live":
            return None
        try:
            return getattr(self._gw_factory(pf), "_a", None)
        except Exception:
            return None

    def _free_balance(self, adapter) -> Optional[float]:
        try:
            st, fl = adapter._request("GET", "/fundlimit")
            if st == 200:
                return float(fl.get("availabelBalance") or fl.get("availableBalance") or 0)
        except Exception:
            logger.exception("seller: fundlimit check failed")
        return None

    def _reconcile_with_broker(self, adapter) -> bool:
        """Startup/daily invariant: every stored leg must exist at the broker.
        Drift => loud alert + entry latch for the day (manage-only mode).
        Returns True when the book is clean (or there is nothing to check)."""
        if not self._mgr.open_spreads:
            return True
        try:
            st, pos = adapter._request("GET", "/positions")
            broker_legs = set()
            for p in (pos if isinstance(pos, list) else []):
                qty = int(p.get("netQty") or 0)
                if qty == 0:
                    continue
                ot = "CE" if str(p.get("drvOptionType", "")).upper().startswith("CALL") else "PE"
                strike = int(float(p.get("drvStrikePrice") or 0))
                broker_legs.add((ot, strike, "BUY" if qty > 0 else "SELL"))
        except Exception:
            logger.exception("seller: broker reconciliation fetch failed — latching entries (fail-closed)")
            self._entry_fail_count = self._entry_fail_latch
            return False
        warnings = self._mgr.reconcile(broker_legs)
        if warnings:
            self._entry_fail_count = self._entry_fail_latch  # manage-only for the day
            self._alert("<b>SELLER BOOK DRIFT</b>\n" + "\n".join(warnings[:4])
                        + "\nEntries latched — reconcile manually.")
            return False
        logger.info("seller reconcile: book matches broker (%d spread(s))", len(self._mgr.open_spreads))
        return True

    def _trade_card(self, decision, pf, expiry, free_margin) -> str:
        """The pre-entry audit card: what we're doing, worst case, and why —
        logged AND alerted BEFORE the first leg goes out (user req 2026-07-09)."""
        leg_txt, credit = [], 0.0
        for l in sorted(decision.legs, key=lambda x: 0 if x.action == "BUY" else 1):
            ltp = pf(l.option_type, l.strike)
            leg_txt.append(f"{l.action} {l.option_type}{l.strike}@{ltp if ltp is not None else '?'}")
            if ltp is not None:
                credit += ltp if l.action == "SELL" else -ltp
        wing = self._width
        max_profit = credit * self._lot
        max_loss = max(0.0, wing - credit) * self._lot
        shorts = [l.strike for l in decision.legs if l.action == "SELL"]
        sense = {"iron_condor": "NEUTRAL", "bull_put": "UP-biased", "bear_call": "DOWN-biased"}.get(
            decision.structure, decision.structure)
        lines = [
            f"<b>SELLER ENTRY CARD</b> {decision.structure} exp={expiry}",
            "  ".join(leg_txt),
            f"est credit {credit:.1f}pt | max profit ~Rs{max_profit:.0f} | max loss ~Rs{max_loss:.0f}",
        ]
        if shorts:
            lines.append(f"breakevens ~{min(shorts) - credit:.0f} / {max(shorts) + credit:.0f}")
        lines.append(
            f"sense {sense} | IV-rank {decision.iv_rank if decision.iv_rank is not None else '?'} "
            f"| regime {decision.regime} | free margin Rs{free_margin if free_margin is not None else '?'}"
        )
        card = "\n".join(lines)
        self._log("entry_card", structure=decision.structure, credit_pts=round(credit, 1),
                  max_profit_rs=round(max_profit), max_loss_rs=round(max_loss),
                  sense=sense, iv_rank=decision.iv_rank, free_margin=free_margin)
        return card

    def on_snapshot(self, snap: dict) -> None:
        if not snap or not snap.get("strikes"):
            return
        day = self._trade_date(snap)
        if day and day != self._cur_day:   # only reset daily gates on a REAL date rollover
            self._cur_day, self._daily_pnl, self._entered_today = day, 0.0, False
            self._entry_fail_count = 0
        # Buyer-coordination flag: maintained every cycle so the buyer's entry
        # gate always sees fresh truth (stale flag auto-ignored on their side).
        try:
            from ..risk.seller_conflict import publish_seller_state
            publish_seller_state(len(self._mgr.open_spreads))
        except Exception:
            pass
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
                sign = "+" if pnl >= 0 else ""
                self._alert(f"<b>SELLER CLOSE {sp.structure}</b>  {sign}₹{pnl:.0f}  [{reason}]  held={held}d")
        # ── entry: once/day, in window, risk-permitting ──
        if self._entered_today:
            return
        if self._entry_fail_count >= self._entry_fail_latch:
            return  # latched for the day (failed opens / reconcile drift) — manage-only
        hh = self._hhmm(snap)
        if not (self._win[0] <= hh <= self._win[1]):
            return
        ok, why = self._risk.can_open(len(self._mgr.open_spreads), self._daily_pnl)
        if not ok:
            return
        ok, why = self._risk.has_conflicting_bn_position(self._db)
        if not ok:
            self._log("entry_skipped_bn_conflict", reason=why)
            return
        if not decision.fires:
            return
        adapter = self._adapter(pf)
        # Daily broker reconciliation before the first entry attempt (live only):
        # never add risk on top of a book that doesn't match the broker.
        if adapter is not None and self._reconciled_day != day:
            self._reconciled_day = day
            if not self._reconcile_with_broker(adapter):
                return
        # Margin pre-check BEFORE leg 1 (2026-07-09: RMS rejected leg 3 of a condor
        # mid-flight; hedges were bought+unwound at real cost, retrying every 30s).
        free = None
        if adapter is not None:
            free = self._free_balance(adapter)
            if free is not None and free < self._min_free_margin:
                self._entry_fail_count += 1
                self._log("entry_skipped_margin", free=free, required=self._min_free_margin,
                          fail_count=self._entry_fail_count)
                if self._entry_fail_count >= self._entry_fail_latch:
                    self._alert(f"<b>SELLER STAND-DOWN</b> free margin Rs{free:.0f} < "
                                f"Rs{self._min_free_margin:.0f} — no entries until tomorrow")
                return
        # Credit floor on the ESTIMATED credit, before any leg exists.
        est_credit = 0.0
        priced = True
        for l in decision.legs:
            ltp = pf(l.option_type, l.strike)
            if ltp is None:
                priced = False
                break
            est_credit += ltp if l.action == "SELL" else -ltp
        if priced and est_credit < self._min_credit_frac * self._width:
            self._log("entry_skipped_thin_credit", est_credit=round(est_credit, 1),
                      floor=round(self._min_credit_frac * self._width, 1),
                      structure=decision.structure)
            return
        # Trade card: the entry must explain itself BEFORE the first leg goes out.
        self._alert(self._trade_card(decision, pf, expiry, free))
        ex = SafeExecutor(self._gw_factory(pf), self._lot, self._width)
        spread = ex.open_spread(decision, expiry, trade_date=str(day))
        if spread is None:
            self._entry_fail_count += 1
            self._log("open_failed", structure=decision.structure, fail_count=self._entry_fail_count)
            if self._entry_fail_count >= self._entry_fail_latch:
                self._alert(f"<b>SELLER STAND-DOWN</b> {self._entry_fail_count} failed open(s) — "
                            f"latched until tomorrow (no retry burn)")
            return
        self._mgr.add(spread)
        self._entered_today = True
        try:
            from ..risk.seller_conflict import publish_seller_state
            publish_seller_state(len(self._mgr.open_spreads))
        except Exception:
            pass
        self._log("open", spread_id=spread.spread_id, structure=spread.structure,
                  credit=spread.entry_credit, legs=[(l.action, l.option_type, l.strike) for l in spread.legs],
                  regime=decision.regime, iv_rank=decision.iv_rank)
        self._mirror_open(spread, decision)
        legs_txt = " ".join(f"{l.action[0]}{l.option_type}{l.strike}" for l in spread.legs)
        self._alert(f"<b>SELLER OPEN {spread.structure}</b>  credit={spread.entry_credit:.0f}  {legs_txt}")

    def run_forever(self, interval_s: float = 30.0) -> None:
        logger.info("SellerRunner loop start (interval=%.0fs, log=%s)", interval_s, self._log_path)
        # Startup reconciliation: a restarted seller holding spreads must prove its
        # book against the broker BEFORE managing anything (Phase 0 invariant).
        if self._mode == "live" and self._mgr.open_spreads:
            adapter = self._adapter(lambda ot, s: None)
            if adapter is not None:
                self._reconcile_with_broker(adapter)
        while True:
            try:
                snap = self._latest()
                if snap:
                    self.on_snapshot(snap)
            except Exception:
                logger.exception("seller loop error")
            time.sleep(interval_s)
