"""Seller-priority coordination flag (Phase 0 of Seller v2, 2026-07-10).

The seller and the buyer are separate containers placing real orders on the SAME
Dhan account. Decision of record (docs/SELLER_V2_PLAN.md): the seller is the
edge-bearing book, the buyer is the research book — so the BUYER yields whenever
the seller has a spread on. Previously only the reverse guard existed, which let
the buyer's churn lock the seller out of its entry window all day (2026-07-09).

Mechanism: a flag file on the shared host .run mount (same pattern as the
operator halt file — no new infra dependency between containers). The seller
maintains it every poll cycle; the buyer checks it at its single entry
chokepoint. Freshness-guarded so a crashed seller can't block the buyer forever.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-instrument flags (2026-07-10, NIFTY seller added): two seller daemons
# must never clobber one shared file — a flat NIFTY seller deleting the flag
# would unblock the buyer while the BN seller still holds a spread.
_FLAG_PREFIX = "seller_active_spread"
# A seller that stopped updating the flag for this long is presumed dead/flat.
_MAX_AGE_S = float(os.getenv("SELLER_CONFLICT_FLAG_MAX_AGE_S", "600") or 600)


def _base_dir() -> Path:
    # NOT STRATEGY_RUN_DIR: that is a per-service subdirectory (/app/.run/strategy_app),
    # but the flag must live at the SHARED mount root so seller (/shared_run) and buyer
    # (/app/.run) resolve the same host file. Caught by the cross-container smoke test.
    return Path(os.getenv("SHARED_RUN_DIR") or "/app/.run")


def flag_path(instrument: str | None = None) -> Path:
    inst = (instrument or os.getenv("STRATEGY_INSTRUMENT") or "BANKNIFTY").upper()
    return _base_dir() / f"{_FLAG_PREFIX}_{inst}.json"


def _enabled() -> bool:
    return str(os.getenv("SELLER_PRIORITY_ENABLED", "1") or "1").strip().lower() in {"1", "true", "yes"}


# ── buyer side ────────────────────────────────────────────────────────────────
def seller_spread_active() -> bool:
    """True when ANY seller (BN or NIFTY — shared account margin) holds a
    spread with a fresh flag — the buyer must not open new positions. Exits
    are never gated by this."""
    if not _enabled():
        return False
    try:
        candidates = list(_base_dir().glob(f"{_FLAG_PREFIX}*.json"))
    except OSError:
        return False
    now = time.time()
    for p in candidates:
        try:
            if now - p.stat().st_mtime <= _MAX_AGE_S:
                return True
            logger.warning("seller_conflict: flag %s is stale (>%ds) — ignoring", p, _MAX_AGE_S)
        except OSError:
            continue
    return False


# ── seller side ───────────────────────────────────────────────────────────────
def publish_seller_state(open_count: int, instrument: str | None = None) -> None:
    """Called by each seller every poll cycle. Maintains (or removes) ITS flag."""
    p = flag_path(instrument)
    try:
        if open_count > 0:
            p.write_text(json.dumps({"open_spreads": open_count, "ts": time.time()}))
        else:
            p.unlink(missing_ok=True)
    except OSError:
        logger.debug("seller_conflict: could not update flag %s", p, exc_info=True)
