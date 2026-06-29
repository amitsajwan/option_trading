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
        lot_size=75,
        strike_step=50,
        expiry_cadence="weekly",   # NIFTY weeklies still listed
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


__all__ = [
    "InstrumentSpec",
    "INSTRUMENTS",
    "get_instrument",
    "known_instruments",
    "PRIMARY_INSTRUMENT",
    "normalize_instrument",
]
