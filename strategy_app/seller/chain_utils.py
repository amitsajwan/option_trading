"""chain_utils — chain-shift-aware option price lookup.

Why this exists:
  Mongo snapshots store only ~25 strikes centered on ATM at capture time. When the
  underlying drifts (overnight or large intraday move), entry strikes fall off the chain.
  The naive response — fall back to entry price or return None and skip — creates phantom
  P&L or silently disables stop-loss monitoring. This module provides a principled fallback:

    1. Exact strike found in chain → use it.
    2. Strike is very far OTM (>OTM_PROXY_THRESHOLD pt from ATM) → 5pt proxy.
       Near-zero time value; the spread legs are both cheap, so the proxy error is tiny.
    3. Strike is moderately OTM or ITM but missing → nearest in-chain strike within
       NEAREST_MAX_DIST pt. Same-type price from an adjacent strike is the best available
       approximation.
    4. No neighbor within NEAREST_MAX_DIST → None (caller must decide).
       For a spread, if BOTH legs return None the spread value stays unknown — the caller
       should log and skip the exit check, NOT silently use last-known price.

  What we deliberately do NOT do:
    - Fall back to entry price (phantom P&L — the classic chain-shift bug)
    - Return 0 for OTM options (triggers early TP on the short legs)
    - Return intrinsic value for OTM options (same as 0 for OTM — misleading)

Usage:
    pf = build_price_fn(snap)
    price = pf("CE", 55200)   # None only if completely unresolvable
"""
from __future__ import annotations

import math
from typing import Callable, Dict, Optional, Tuple

OTM_PROXY_THRESHOLD = 600   # pt from ATM beyond which we treat as near-worthless
OTM_PROXY_PRICE     = 5.0   # proxy price for very-far-OTM strikes
NEAREST_MAX_DIST    = 300   # pt; only use nearest-neighbor within this band


def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError):
        return None


def build_price_fn(snap: dict) -> Callable[[str, int], Optional[float]]:
    """Return a price-lookup function for the given snapshot.

    The returned callable takes (option_type: str, strike: int) and returns a price
    in points (float) or None if completely unresolvable.

    Chain-shift fallback is applied automatically — callers do not need to handle it.
    """
    # Build chain index: strike → (ce_ltp, pe_ltp)
    chain: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for row in (snap.get("strikes") or []):
        k = _f(row.get("strike"))
        if k is None:
            continue
        chain[int(k)] = (_f(row.get("ce_ltp")), _f(row.get("pe_ltp")))

    # ATM from chain_aggregates (needed to detect far-OTM fallback zone)
    ca = snap.get("chain_aggregates") or {}
    try:
        atm = float(ca.get("atm_strike") or 0) or 0.0
    except (TypeError, ValueError):
        atm = 0.0

    def price(ot: str, strike: int) -> Optional[float]:
        # ── 1. exact hit ─────────────────────────────────────────────────────
        pair = chain.get(strike)
        if pair is not None:
            v = pair[0] if ot == "CE" else pair[1]
            if v is not None:
                return v

        # ── 2. far-OTM proxy ─────────────────────────────────────────────────
        if atm > 0:
            otm_dist = (strike - atm) if ot == "CE" else (atm - strike)
            if otm_dist > OTM_PROXY_THRESHOLD:
                return OTM_PROXY_PRICE

        # ── 3. nearest in-chain neighbor ─────────────────────────────────────
        best_price: Optional[float] = None
        best_dist: int = NEAREST_MAX_DIST + 1
        for s, pair in chain.items():
            d = abs(s - strike)
            if d < best_dist:
                v = pair[0] if ot == "CE" else pair[1]
                if v is not None:
                    best_price, best_dist = v, d
        if best_price is not None:
            return best_price

        # ── 4. unresolvable ──────────────────────────────────────────────────
        return None

    return price
