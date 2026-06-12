"""PositionManager (T5) + RiskGates (T6).

PositionManager: durable open-spread store (survives restarts), daily mark-to-market,
exit rules (50% TP / 2x stop / DTE), and broker reconciliation. This is the safety-
critical layer for POSITIONAL (multi-day) holding — losing track of an open spread is
the real danger, so the store is on disk and reconciled against the broker each cycle.

RiskGates: 1 concurrent position (configurable), defined-risk cap, daily-loss halt.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date
from typing import Callable, Optional

from .executor import FilledLeg, OpenSpread

logger = logging.getLogger(__name__)

# exit thresholds (per-unit, fraction of credit / DTE)
TP_FRAC = float(os.getenv("SELLER_TP_FRAC", "0.50") or 0.50)        # take 50% of credit
STOP_MULT = float(os.getenv("SELLER_STOP_MULT", "2.0") or 2.0)      # stop at 2x credit
DTE_EXIT = int(os.getenv("SELLER_DTE_EXIT", "7") or 7)              # force-exit before gamma week
MAX_HOLD_DAYS = int(os.getenv("SELLER_MAX_HOLD_DAYS", "5") or 5)    # hold-horizon cap


class PositionStore:
    """Durable JSON store of open spreads. One file, atomic writes, restart-safe."""

    def __init__(self, path: Optional[str] = None):
        self._path = path or os.path.join(os.getenv("STRATEGY_RUN_DIR", "/tmp"), "seller_open_spreads.json")

    def load(self) -> list[OpenSpread]:
        try:
            with open(self._path, "r") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for d in raw:
            legs = [FilledLeg(**l) for l in d.pop("legs", [])]
            out.append(OpenSpread(legs=legs, **d))
        return out

    def save(self, spreads: list[OpenSpread]) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump([asdict(s) for s in spreads], fh)
        os.replace(tmp, self._path)   # atomic


class PositionManager:
    def __init__(self, store: Optional[PositionStore] = None):
        self._store = store or PositionStore()
        self._open: list[OpenSpread] = self._store.load()   # restart recovery: resume management
        if self._open:
            logger.info("seller: recovered %d open spread(s) from store", len(self._open))

    @property
    def open_spreads(self) -> list[OpenSpread]:
        return list(self._open)

    def add(self, spread: OpenSpread) -> None:
        self._open.append(spread)
        self._store.save(self._open)

    def remove(self, spread_id: str) -> None:
        self._open = [s for s in self._open if s.spread_id != spread_id]
        self._store.save(self._open)

    # ── mark-to-market + exit decision ───────────────────────────────────────
    @staticmethod
    def spread_value(spread: OpenSpread, price_fn: Callable[[str, int], Optional[float]]) -> Optional[float]:
        """Current value (points) = sum of EACH vertical (short ltp - long ltp) bounded to
        [0, that vertical's width]. Per-vertical bounding is REQUIRED (a condor is two
        spreads) — a single raw sum can go negative or exceed risk (the paper-validation bug)."""
        total = 0.0
        for ot in ("CE", "PE"):
            legs = [fl for fl in spread.legs if fl.option_type == ot]
            shorts = [fl for fl in legs if fl.action == "SELL"]
            longs = [fl for fl in legs if fl.action == "BUY"]
            if not shorts or not longs:
                continue
            s, lg = shorts[0], longs[0]
            sp, lp = price_fn(ot, s.strike), price_fn(ot, lg.strike)
            if sp is None or lp is None:
                return None
            w = abs(s.strike - lg.strike)
            total += max(0.0, min(float(w), sp - lp))
        return total

    def check_exit(self, spread: OpenSpread, value: float, days_held: int, dte: Optional[int]) -> Optional[str]:
        credit = spread.entry_credit
        if value <= TP_FRAC * credit:
            return "take_profit_50"
        if value >= STOP_MULT * credit:
            return "stop_2x"
        if dte is not None and dte <= DTE_EXIT:
            return "dte_exit"
        if days_held >= MAX_HOLD_DAYS:
            return "max_hold"
        return None

    # ── broker reconciliation (live only) ────────────────────────────────────
    def reconcile(self, broker_legs: set[tuple[str, int, str]]) -> list[str]:
        """Compare our store vs the broker's actual legs. Returns warnings (drift)."""
        warnings = []
        for s in self._open:
            for fl in s.legs:
                key = (fl.option_type, fl.strike, fl.action)
                if key not in broker_legs:
                    warnings.append(f"DRIFT: stored leg {key} (spread {s.spread_id}) not at broker")
        if warnings:
            logger.error("seller reconcile: %d drift warning(s) — %s", len(warnings), warnings[:3])
        return warnings


class RiskGates:
    def __init__(self):
        self._max_concurrent = int(os.getenv("SELLER_MAX_CONCURRENT", "1") or 1)
        self._daily_loss_cap = float(os.getenv("SELLER_DAILY_LOSS_CAP_RS", "6000") or 6000)

    def can_open(self, open_count: int, daily_pnl_rs: float) -> tuple[bool, str]:
        if open_count >= self._max_concurrent:
            return False, f"max concurrent {self._max_concurrent} reached"
        if daily_pnl_rs <= -self._daily_loss_cap:
            return False, f"daily loss cap ₹{self._daily_loss_cap:.0f} hit ({daily_pnl_rs:.0f})"
        return True, "ok"
