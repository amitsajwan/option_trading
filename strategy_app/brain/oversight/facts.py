"""SENSE + CALCULATOR — deterministic market facts for the oversight brain.

Pulls the REAL numbers from a snapshot (prev-day/week levels, PCR, max-pain,
OI walls, VIX, futures) and computes derived measures (location zone, distance
to levels, move vs prev close). No LLM, no external fetch — these are the
grounded facts the brain reasons over. Prior FII and the event calendar are
optional external overlays.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _snapshot_body(snap: Any) -> dict:
    """Accept a raw mongo doc, a {'snapshot': ...} wrapper, or the inner snapshot."""
    if not isinstance(snap, dict):
        return {}
    payload = snap.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("snapshot"), dict):
        return payload["snapshot"]
    if isinstance(snap.get("snapshot"), dict):
        return snap["snapshot"]
    return snap


def location_zone(px: Optional[float], pdh: Optional[float], pdl: Optional[float]) -> str:
    """Where is price relative to prev-day high/low (band 0.15%)."""
    if px is None or pdh is None or pdl is None:
        return "unknown"
    band = 0.0015 * px
    if px > pdh:
        return "above_PDH"
    if px >= pdh - band:
        return "near_PDH"
    if px < pdl:
        return "below_PDL"
    if px <= pdl + band:
        return "near_PDL"
    return "mid_range"


@dataclass
class MarketFacts:
    """The grounded, deterministic facts one cycle reasons over."""

    timestamp: str = ""
    trade_date: str = ""
    fut_price: Optional[float] = None
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None
    prev_day_close: Optional[float] = None
    week_high: Optional[float] = None
    week_low: Optional[float] = None
    gap_pct: Optional[float] = None
    pcr: Optional[float] = None
    max_pain: Optional[float] = None
    ce_oi_top_strike: Optional[float] = None
    pe_oi_top_strike: Optional[float] = None
    vix: Optional[float] = None
    dist_to_pdh: Optional[float] = None      # +above / -below, points
    dist_to_pdl: Optional[float] = None
    fut_vs_prev_close_pct: Optional[float] = None
    location_zone: str = "unknown"
    prior_fii_cr: Optional[float] = None     # external overlay (prior session)
    events: list = field(default_factory=list)  # external overlay (curated)

    @classmethod
    def from_snapshot(
        cls,
        snap: Any,
        *,
        prior_fii_cr: Any = None,
        events: Optional[list] = None,
    ) -> "MarketFacts":
        s = _snapshot_body(snap)
        fb = s.get("futures_bar") or {}
        sl = s.get("session_levels") or {}
        ca = s.get("chain_aggregates") or {}
        vx = s.get("vix_context") or {}

        px = _f(fb.get("fut_close"))
        pdh, pdl, pdc = _f(sl.get("prev_day_high")), _f(sl.get("prev_day_low")), _f(sl.get("prev_day_close"))
        pcr = _f(ca.get("pcr"))
        if pcr is None:
            pcr = _f(sl.get("prev_day_pcr"))
        mp = _f(ca.get("max_pain"))
        if mp is None:
            mp = _f(sl.get("prev_day_max_pain"))

        f = cls(
            timestamp=str(s.get("timestamp") or ""),
            trade_date=str(s.get("trade_date") or ""),
            fut_price=px,
            prev_day_high=pdh, prev_day_low=pdl, prev_day_close=pdc,
            week_high=_f(sl.get("week_high")), week_low=_f(sl.get("week_low")),
            gap_pct=_f(sl.get("overnight_gap")),
            pcr=pcr, max_pain=mp,
            ce_oi_top_strike=_f(ca.get("ce_oi_top_strike")),
            pe_oi_top_strike=_f(ca.get("pe_oi_top_strike")),
            vix=_f(vx.get("vix") or vx.get("india_vix") or vx.get("value")),
            prior_fii_cr=_f(prior_fii_cr),
            events=list(events or []),
        )
        if px is not None and pdh is not None:
            f.dist_to_pdh = round(px - pdh, 1)
        if px is not None and pdl is not None:
            f.dist_to_pdl = round(px - pdl, 1)
        if px is not None and pdc not in (None, 0):
            f.fut_vs_prev_close_pct = round((px - pdc) / pdc, 5)
        f.location_zone = location_zone(px, pdh, pdl)
        return f

    def to_prompt_dict(self) -> dict[str, Any]:
        """Compact, non-null facts to hand the LLM (the grounded inputs)."""
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "", [])}


__all__ = ["MarketFacts", "location_zone"]
