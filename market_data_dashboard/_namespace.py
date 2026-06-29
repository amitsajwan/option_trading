from __future__ import annotations

from typing import Literal, Optional

from contracts_app import resolve_namespace

NamespaceKind = Literal["live", "oos", "sim"]
BASE_SNAPSHOTS = "phase1_market_snapshots"
BASE_VOTES = "strategy_votes"
BASE_SIGNALS = "trade_signals"
BASE_POSITIONS = "strategy_positions"
BASE_DECISION_TRACES = "strategy_decision_traces"


def normalize_kind(kind: Optional[str], *, default: NamespaceKind = "live") -> NamespaceKind:
    raw = str(kind or "").strip().lower()
    if raw in {"", "none"}:
        return default
    if raw == "historical":
        return "oos"
    if raw in {"live", "oos", "sim"}:
        return raw  # type: ignore[return-value]
    raise ValueError("kind must be one of: live, oos, sim")


def collection_for(
    base: str,
    *,
    kind: Optional[str] = None,
    run_id: Optional[str] = None,
    instrument: Optional[str] = None,
) -> str:
    ns = resolve_namespace(normalize_kind(kind), run_id=run_id, instrument=instrument)
    return ns.collection_for(base)

