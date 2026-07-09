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

_FLAG_BASENAME = "seller_active_spread.json"
# A seller that stopped updating the flag for this long is presumed dead/flat.
_MAX_AGE_S = float(os.getenv("SELLER_CONFLICT_FLAG_MAX_AGE_S", "600") or 600)


def flag_path() -> Path:
    base = os.getenv("SHARED_RUN_DIR") or os.getenv("STRATEGY_RUN_DIR") or "/app/.run"
    return Path(base) / _FLAG_BASENAME


def _enabled() -> bool:
    return str(os.getenv("SELLER_PRIORITY_ENABLED", "1") or "1").strip().lower() in {"1", "true", "yes"}


# ── buyer side ────────────────────────────────────────────────────────────────
def seller_spread_active() -> bool:
    """True when the seller currently holds a spread (fresh flag) — the buyer
    must not open new positions (shared margin + the seller is the priority
    book). Exits are never gated by this."""
    if not _enabled():
        return False
    p = flag_path()
    try:
        st = p.stat()
    except OSError:
        return False
    if time.time() - st.st_mtime > _MAX_AGE_S:
        logger.warning("seller_conflict: flag %s is stale (>%ds) — ignoring", p, _MAX_AGE_S)
        return False
    return True


# ── seller side ───────────────────────────────────────────────────────────────
def publish_seller_state(open_count: int) -> None:
    """Called by the seller every poll cycle. Maintains (or removes) the flag."""
    p = flag_path()
    try:
        if open_count > 0:
            p.write_text(json.dumps({"open_spreads": open_count, "ts": time.time()}))
        else:
            p.unlink(missing_ok=True)
    except OSError:
        logger.debug("seller_conflict: could not update flag %s", p, exc_info=True)
