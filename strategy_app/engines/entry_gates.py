"""Env-driven entry gates for session-level filtering.

Two independent gates, both controlled by env vars and applied at the top of
`_process_entry_votes` in the deterministic engine:

1. **Time-window gate** — `ENTRY_TIME_WINDOWS="HH:MM-HH:MM,HH:MM-HH:MM"`
   Skip entries outside the listed IST time windows. Empty/unset = disabled.

2. **Daily regime gate** — `ENTRY_REGIME_TAGGER=<name>` +
   `ENTRY_REGIME_ALLOWED_TAGS="bear,chop"`. Computes a session-once daily
   regime tag from the snapshot and skips entries when the tag isn't in the
   allowed list. Empty/unset = disabled.

Background (E8 analysis 2026-05-25): CE on long ATM BN at 1-min behaves like
a mean-reversion timer — wins on bear+chop days, loses on bull days. Stacking
the regime gate with the time-window gate gives the first config in the
research arc with bootstrap PF lower bound > 1.0 on both Ref and E2.
"""
from __future__ import annotations

import os
from typing import Optional

from ..market.snapshot_accessor import SnapshotAccessor


_BULL = "bull"
_BEAR = "bear"
_CHOP = "chop"
_UNKNOWN = "unknown"


def _env_str(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _parse_windows(raw: str) -> list[tuple[int, int]]:
    """Parse 'HH:MM-HH:MM,HH:MM-HH:MM' → [(start_min_of_day, end_min_of_day)]."""
    windows: list[tuple[int, int]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece or "-" not in piece:
            continue
        try:
            a, b = piece.split("-", 1)
            sh, sm = a.split(":")
            eh, em = b.split(":")
            start = int(sh) * 60 + int(sm)
            end = int(eh) * 60 + int(em)
            if end > start:
                windows.append((start, end))
        except (ValueError, IndexError):
            continue
    return windows


def is_in_configured_time_window(snap: SnapshotAccessor) -> bool:
    """True if no windows configured OR snap's time-of-day falls in one of them."""
    raw = _env_str("ENTRY_TIME_WINDOWS")
    if not raw:
        return True
    windows = _parse_windows(raw)
    if not windows:
        return True
    ts = snap.timestamp
    if ts is None:
        return False
    mins = int(ts.hour) * 60 + int(ts.minute)
    return any(start <= mins < end for start, end in windows)


# ---------------------------------------------------------------------------
# Regime taggers (pure functions of session-snapshot fields)
# ---------------------------------------------------------------------------
def _tag_gap(overnight_gap: Optional[float], threshold: float) -> str:
    if overnight_gap is None:
        return _UNKNOWN
    if overnight_gap > threshold:
        return _BULL
    if overnight_gap < -threshold:
        return _BEAR
    return _CHOP


def _tag_open_vs_prev(fut_open: Optional[float], prev_close: Optional[float], threshold: float) -> str:
    if fut_open is None or prev_close is None or prev_close <= 0:
        return _UNKNOWN
    diff = (float(fut_open) - float(prev_close)) / float(prev_close)
    if diff > threshold:
        return _BULL
    if diff < -threshold:
        return _BEAR
    return _CHOP


def _tag_orb(orh_broken: bool, orl_broken: bool) -> str:
    if orh_broken and not orl_broken:
        return _BULL
    if orl_broken and not orh_broken:
        return _BEAR
    return _CHOP


def _tag_pcr(pcr: Optional[float]) -> str:
    if pcr is None:
        return _UNKNOWN
    # Low PCR = more call OI than put = bullish
    if pcr < 0.7:
        return _BULL
    if pcr > 1.2:
        return _BEAR
    return _CHOP


def compute_regime_tag(tagger: str, snap: SnapshotAccessor) -> str:
    """Compute daily regime tag for the session.

    Returns 'bull' / 'bear' / 'chop' / 'unknown'. 'unknown' means tagger
    couldn't decide (e.g. ORB not yet resolved at <09:45, or missing data).
    Caller should treat 'unknown' as 'not yet ready, do not cache'.
    """
    name = (tagger or "").strip().lower()
    if not name:
        return _UNKNOWN
    if name == "gap_03pct":
        return _tag_gap(snap.overnight_gap, 0.003)
    if name == "open_vs_prev_02pct":
        return _tag_open_vs_prev(snap.fut_open, snap.prev_day_close, 0.002)
    if name == "orb_at_945":
        if not (snap.orh_broken or snap.orl_broken):
            return _UNKNOWN  # OR not yet resolved
        return _tag_orb(snap.orh_broken, snap.orl_broken)
    if name == "pcr_prev_day":
        return _tag_pcr(snap.prev_day_pcr)
    if name == "combined_majority":
        # 3-source majority vote: gap, open-vs-prev, ORB. PCR as tiebreak.
        # ORB is the slowest signal — if not yet resolved, tag is unknown.
        if not (snap.orh_broken or snap.orl_broken):
            return _UNKNOWN
        votes = [
            _tag_gap(snap.overnight_gap, 0.002),
            _tag_open_vs_prev(snap.fut_open, snap.prev_day_close, 0.0015),
            _tag_orb(snap.orh_broken, snap.orl_broken),
        ]
        bull_n = sum(1 for v in votes if v == _BULL)
        bear_n = sum(1 for v in votes if v == _BEAR)
        if bull_n >= 2:
            return _BULL
        if bear_n >= 2:
            return _BEAR
        pcr_tag = _tag_pcr(snap.prev_day_pcr)
        if pcr_tag in (_BULL, _BEAR):
            return pcr_tag
        return _CHOP
    return _UNKNOWN


def is_session_regime_allowed(cached_tag: Optional[str]) -> bool:
    """True if no regime gate configured OR cached_tag is in allowed list.

    A cached_tag of None or 'unknown' means the tag wasn't computable yet — in
    that case the gate BLOCKS (we'd rather skip entries on undecided days).
    """
    allowed_raw = _env_str("ENTRY_REGIME_ALLOWED_TAGS")
    if not allowed_raw:
        return True
    if not cached_tag or cached_tag == _UNKNOWN:
        return False
    allowed = {tag.strip().lower() for tag in allowed_raw.split(",") if tag.strip()}
    return cached_tag.lower() in allowed
