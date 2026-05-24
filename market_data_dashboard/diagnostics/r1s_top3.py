"""Diagnostics for R1S_TOP3_SHORT_CE votes (replay / live)."""
from __future__ import annotations

from typing import Any, Optional


def _coerce_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def build_r1s_top3_diagnostics(*, votes_coll: Any, day_query: dict[str, Any]) -> dict[str, Any]:
    """Aggregate R1S-specific fields from persisted vote payloads."""
    projection = {"_id": 0, "strategy": 1, "payload.vote": 1}
    docs = list(votes_coll.find(day_query, projection))
    entry_candidates = 0
    short_ce_flags = 0
    scores: list[float] = []
    rank_slots: list[int] = []
    for doc in docs:
        if str(doc.get("strategy") or "").strip().upper() != "R1S_TOP3_SHORT_CE":
            continue
        vote = ((doc.get("payload") or {}).get("vote")) if isinstance(doc.get("payload"), dict) else {}
        vote = vote if isinstance(vote, dict) else {}
        if str(vote.get("signal_type") or "").strip().upper() != "ENTRY":
            continue
        entry_candidates += 1
        raw = vote.get("raw_signals") if isinstance(vote.get("raw_signals"), dict) else {}
        if _coerce_bool(raw.get("_r1s_short_ce")):
            short_ce_flags += 1
        try:
            scores.append(float(raw.get("_r1s_top3_score")))
        except (TypeError, ValueError):
            pass
        try:
            rank_slots.append(int(raw.get("_r1s_top3_rank_slot")))
        except (TypeError, ValueError):
            pass
    return {
        "entry_votes_day": entry_candidates,
        "short_ce_tagged": short_ce_flags,
        "avg_top3_score": (sum(scores) / len(scores)) if scores else None,
        "max_top3_score": max(scores) if scores else None,
        "entries_taken_max_slot": max(rank_slots) if rank_slots else None,
    }
