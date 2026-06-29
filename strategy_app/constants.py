"""Shared constants for strategy_app to eliminate magic-number duplication."""

from __future__ import annotations

import os
from typing import Optional

# Market
# Env-configurable so live trading can use the current exchange lot size while
# historical replays keep their era-appropriate value (mirrors snapshot_app's
# market_snapshot.py, which already reads this same env var). NSE lot size for
# BANKNIFTY is 30 as of 2026; default stays 15 to preserve existing replays/tests.
BANKNIFTY_LOT_SIZE = int(os.getenv("BANKNIFTY_LOT_SIZE") or "15")


def resolve_lot_size(instrument: Optional[str] = None, *, primary_default: Optional[int] = None) -> int:
    """Lot size for the active instrument — the one source runtime code should use.

    Precedence (keeps every existing BankNifty deployment/replay/test identical):
      1. ``STRATEGY_LOT_SIZE`` env  — explicit global override, wins everywhere.
      2. Primary instrument (BANKNIFTY): the caller's ``primary_default`` if given
         (lets each call site keep its own historical BankNifty default — 15 for
         replay-era cost math, 30 for the live cost model), otherwise the legacy
         ``BANKNIFTY_LOT_SIZE`` constant (default 15, env-overridable).
      3. Otherwise: the InstrumentSpec registry (NIFTY=75, ...).

    ``instrument`` defaults to STRATEGY_INSTRUMENT via current_instrument().
    """
    override = os.getenv("STRATEGY_LOT_SIZE", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    # Local imports avoid a hard contracts_app dependency at module import time.
    try:
        from contracts_app import current_instrument, normalize_instrument, get_instrument
    except Exception:
        return primary_default if primary_default is not None else BANKNIFTY_LOT_SIZE
    inst = normalize_instrument(instrument) if instrument else current_instrument()
    if inst == "BANKNIFTY":
        # Preserve each call site's legacy BankNifty default.
        return primary_default if primary_default is not None else BANKNIFTY_LOT_SIZE
    try:
        return int(get_instrument(inst).lot_size)
    except Exception:
        return primary_default if primary_default is not None else BANKNIFTY_LOT_SIZE

# Session timing (minutes since midnight)
SOFT_CLOSE_MINUTE = 15 * 60          # 15:00 IST
HARD_CLOSE_MINUTE = 15 * 60 + 15   # 15:15 IST

# Numeric safety
PRICE_EPS = 1e-9

# Engine thresholds
MIN_ENTRY_CONFIDENCE = 0.65
EXIT_CONFIDENCE = 0.65

# Capital / risk defaults
DEFAULT_CAPITAL_ALLOCATED = 500_000.0
DEFAULT_RISK_PER_TRADE_PCT = 0.005
DEFAULT_MAX_DAILY_LOSS_PCT = 0.02
DEFAULT_MAX_SESSION_TRADES = 6
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
DEFAULT_MAX_LOTS_PER_TRADE = 5

# Profile
RISK_PROFILE_AGGRESSIVE_SAFE_V1 = "aggressive_safe_v1"
