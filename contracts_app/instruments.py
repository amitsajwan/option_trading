"""Single source of truth for per-instrument facts (symbol, ids, lot, cadence).

THE registry every layer imports so ingestion, snapshot, strategy, execution,
and the training pipeline agree on instrument facts — the same convergence
discipline applied to feature_engine, here for instrument metadata. Nothing in
the codebase should hardcode ``IDX_BANKNIFTY="25"``, ``lot_size=30``, or
``strike_step=100`` inline; resolve through ``get_instrument(name)`` instead.

This mirrors the ``InstrumentConfig`` previously defined only inside
ml_pipeline_2/scripts/dhan_data_pipeline.py — promoted here so live and
training share one definition (kills ingest/strategy/train instrument skew).

Pairs with contracts_app.sim_namespace, which uses the instrument NAME as the
second axis of the namespace. The primary instrument (BANKNIFTY) is the
unsuffixed default there; this registry is naming-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class InstrumentSpec:
    """Static facts about one tradeable index instrument."""

    name: str                    # canonical UPPER name, e.g. "BANKNIFTY"
    index_security_id: str       # Dhan security id for the index (IDX_I segment)
    lot_size: int                # contract multiplier (current regime)
    strike_step: int             # minimum strike increment, points
    # Expiry cadence drives DTE / expiry calendar selection:
    #   "weekly"  -> every Thursday (NIFTY; BankNifty pre-Nov-2024)
    #   "monthly" -> last Thursday of month (BankNifty post-Nov-2024)
    expiry_cadence: str = "weekly"
    fno_segment: str = "NSE_FNO"
    index_segment: str = "IDX_I"
    # VIX is shared across instruments but kept here so a single spec fully
    # describes what a stack needs to subscribe.
    vix_security_id: str = "21"
    vix_segment: str = "IDX_I"


# Keep this table aligned with ml_pipeline_2/scripts/dhan_data_pipeline.py
# (INSTRUMENTS). Values confirmed Jun 2026.
INSTRUMENTS: Dict[str, InstrumentSpec] = {
    "BANKNIFTY": InstrumentSpec(
        name="BANKNIFTY",
        index_security_id="25",   # ~58,400 in Jun 2026
        lot_size=30,
        strike_step=100,
        expiry_cadence="monthly",  # weeklies discontinued ~Nov 2024
    ),
    "NIFTY": InstrumentSpec(
        name="NIFTY",
        index_security_id="13",   # ~24,000 in Jun 2026
        # NOTE: NSE changed NIFTY lot size from 75 → 25 in late 2024. This static
        # value is retained for historical training data (pre-2025 replays used 75).
        # Live runtime uses NIFTY_LOT_SIZE env var via strategy_app.constants.resolve_lot_size().
        # Execution adapter reads the authoritative per-expiry value from Dhan scrip master.
        lot_size=75,
        strike_step=50,
        expiry_cadence="weekly",   # NIFTY weeklies still listed
    ),
    "FINNIFTY": InstrumentSpec(
        # BUG (found 2026-08-02): this entry was missing entirely. get_instrument()
        # correctly raises for unknown names ("fail loud"), but
        # ingestion_app.dhan_data_service.get_historical_day() wraps that call in a
        # bare except-and-fall-back-to-self._active_spec() -- which silently
        # substituted the CONTAINER's own default instrument (BankNifty, since this
        # only runs inside the BankNifty-flavored ingestion_app) for every FINNIFTY
        # historical-day request. Every FINNIFTY backfill run through
        # hist_backfill.py before this fix (incl. two full multi-hundred-day runs
        # on 2026-07-31 and 2026-08-01) silently ingested BankNifty's index,
        # futures, AND options data mislabeled as FINNIFTY and needs re-running.
        # Facts confirmed live 2026-07-26 (see ml_pipeline_2/scripts/dhan_data_pipeline.py).
        name="FINNIFTY",
        index_security_id="27",   # ~26,300 confirmed live 2026-07-26, scrip master
        lot_size=60,               # confirmed live scrip master 2026-07-26
        strike_step=50,            # near-ATM step confirmed from live chain
        expiry_cadence="monthly",  # only monthly expiries listed as of 2026-07-26
    ),
    "SENSEX": InstrumentSpec(
        # First non-NSE instrument (BSE). Confirmed live 2026-08-07 via scrip
        # master + historical API probe (see docs/ONBOARDING_INSTRUMENT.md
        # section 10 for the full record):
        #  - security_id=51, SEM_CUSTOM_SYMBOL="Sensex", index segment IDX_I
        #    (same as NSE indices -- no separate BSE index segment).
        #  - TRAP: "SENSEX" and "SENSEX50" are two different, unrelated index
        #    families sharing a string prefix (SENSEX50 lot=75, a different
        #    index). Every symbol-family filter must use an exact
        #    leading-token match (symbol.split("-",1)[0]), never a prefix
        #    match -- see execution_app/adapter/dhan.py's _ScripMaster for
        #    the proven-safe pattern.
        #  - Expiry weekday: Thursday, universally, with zero exceptions
        #    across every listed expiry (near-term weeklies through
        #    2031-dated quarterly contracts). Simpler than NIFTY's Sep-2025
        #    Thu->Tue cutover -- see hist_backfill.sensex_expiry().
        #  - Historical data (index/futures daily+intraday, options-history
        #    endpoint) confirmed available identically to NSE -- no BSE-
        #    specific gap found; immediate backfill-and-train is safe.
        name="SENSEX",
        index_security_id="51",
        lot_size=20,                # confirmed live scrip master 2026-08-07 (SEM_LOT_UNITS on OPTIDX rows)
        strike_step=100,            # confirmed via consecutive listed strikes
        expiry_cadence="weekly",    # near-term weeklies listed, same shape as NIFTY
        fno_segment="BSE_FNO",
    ),
}

# The primary instrument is re-exported from sim_namespace so callers have one
# import for both the registry and the namespace default. Imported lazily-safe.
try:
    from .sim_namespace import PRIMARY_INSTRUMENT, normalize_instrument
except Exception:  # pragma: no cover - defensive for partial imports
    PRIMARY_INSTRUMENT = "BANKNIFTY"

    def normalize_instrument(instrument: Optional[str]) -> str:  # type: ignore[misc]
        raw = str(instrument or "").strip().upper()
        return raw or PRIMARY_INSTRUMENT


def get_instrument(name: Optional[str]) -> InstrumentSpec:
    """Return the spec for ``name`` (case-insensitive); defaults to primary.

    Unknown non-empty names raise — fail loud rather than silently trading the
    wrong contract size.
    """
    canon = normalize_instrument(name)
    spec = INSTRUMENTS.get(canon)
    if spec is None:
        raise KeyError(
            f"unknown instrument {canon!r}; known: {sorted(INSTRUMENTS)}"
        )
    return spec


def known_instruments() -> list[str]:
    """Canonical names of all registered instruments."""
    return sorted(INSTRUMENTS)


# Dhan's numeric ws-feed segment codes (dhanhq.MarketFeed.*) and plain
# exchange codes (as they appear in the scrip master's SEM_EXM_EXCH_ID
# column), keyed by InstrumentSpec.fno_segment. Added 2026-08-07 for SENSEX
# (BSE_FNO) -- the first non-NSE instrument. Add a row here, and nowhere
# else, when a new exchange segment is onboarded; every live-path call site
# (order placement, historical/quote fetch, WS subscription, scrip-master
# filtering) must derive from these tables via get_instrument(name), never
# hardcode "NSE_FNO"/"BSE_FNO" again -- that was exactly the bug this fixes
# (the field existed and defaulted to "NSE_FNO" the whole time but was never
# read anywhere in the live path).
FNO_SEGMENT_WS_CODE: Dict[str, int] = {"NSE_FNO": 2, "BSE_FNO": 8}
FNO_SEGMENT_EXCHANGE: Dict[str, str] = {"NSE_FNO": "NSE", "BSE_FNO": "BSE"}


def fno_segment_ws_code(name: Optional[str]) -> int:
    """Numeric WS-feed segment code for ``name``'s fno_segment. Fails loud on
    an unregistered segment rather than silently defaulting to NSE's code."""
    spec = get_instrument(name)
    try:
        return FNO_SEGMENT_WS_CODE[spec.fno_segment]
    except KeyError:
        raise KeyError(
            f"no ws segment code registered for fno_segment={spec.fno_segment!r} "
            f"(instrument={spec.name}); add it to FNO_SEGMENT_WS_CODE"
        ) from None


__all__ = [
    "InstrumentSpec",
    "INSTRUMENTS",
    "get_instrument",
    "known_instruments",
    "PRIMARY_INSTRUMENT",
    "normalize_instrument",
    "FNO_SEGMENT_WS_CODE",
    "FNO_SEGMENT_EXCHANGE",
    "fno_segment_ws_code",
]
