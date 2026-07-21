"""Central namespace resolver for live / oos-historical / sim modes.

THE single source of truth for collection names, Redis stream/topic names,
state-key prefixes, filesystem dirs, and consumer-lock keys across the
three system modes.

Design rule: nothing in the codebase should string-concatenate
``"phase1_market_snapshots_sim"`` or ``"stream:snapshots:sim:..."``
or ``"/app/.run/strategy_app_sim/..."`` directly. Always go through
``resolve_namespace(kind).{collection_for,stream_for,...}``. This keeps
the three modes parallel and lets us add a fourth mode (or rename a
collection) by editing one file.

See also:
    docs/SCRUM_BOARD_SIM_REPLAY.md  (SIM-1)
    memory/project_sim_replay_design_2026-05-27  (design doc)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

Kind = Literal["live", "oos", "sim"]
Transport = Literal["pubsub", "streams"]

_VALID_KINDS: tuple[Kind, ...] = ("live", "oos", "sim")

# Base names of mongo collections that get suffixed per kind.
# Callers pass one of these to ``collection_for``; passing anything else
# returns the input unchanged (so non-namespaced collections like
# ``strategy_eval_runs`` keep working without special-casing).
_NAMESPACED_BASES: frozenset[str] = frozenset(
    {
        # Legacy snapshot + strategy collections
        "phase1_market_snapshots",
        "strategy_votes",
        "trade_signals",
        "strategy_positions",
        "strategy_decision_traces",
        "market_depth_ticks",
        # Phase 2 stream-native decision pipeline collections
        # stream_for() uses these slugs directly; collection_for() appends the kind suffix.
        "regime_decisions",
        "entry_decisions",
        "direction_decisions",
        "depth_decisions",
        "strike_decisions",
        "risk_decisions",
        "execution_events",
    }
)

_LIVE_RUN_DIR = Path("/app/.run/strategy_app")
_OOS_RUN_DIR = Path("/app/.run/strategy_app_historical")
_SIM_RUN_DIR_BASE = Path("/app/.run/strategy_app_sim")


@dataclass(frozen=True)
class Namespace:
    """Resolved naming surface for one mode (+ optional run_id for sim)."""

    kind: Kind
    run_id: Optional[str] = None

    def __post_init__(self) -> None:  # pragma: no cover - trivial
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"unknown kind={self.kind!r}; expected one of {_VALID_KINDS}"
            )
        if self.kind == "sim" and not self.run_id:
            raise ValueError("sim namespace requires a non-empty run_id")

    # ── collection naming ────────────────────────────────────────────────
    def collection_for(self, base: str) -> str:
        """Return the Mongo collection name for ``base`` in this namespace.

        ``base`` not in the namespaced-bases set is returned unchanged —
        this lets shared collections (eg. ``strategy_eval_runs``) pass through
        without callers needing to special-case them.
        """
        key = str(base or "").strip()
        if not key:
            raise ValueError("collection base name is required")
        if key not in _NAMESPACED_BASES:
            return key
        if self.kind == "live":
            return key
        if self.kind == "oos":
            return f"{key}_historical"
        return f"{key}_sim"

    # ── transport / topic / stream naming ────────────────────────────────
    def transport(self) -> Transport:
        """Return the transport for this namespace.

        D1 (arch/streams-loose-coupling): returns ``streams`` for all modes.
        Set ``NAMESPACE_STREAMS_TRANSPORT=false`` to revert to legacy behaviour
        (``pubsub`` for live/oos, ``streams`` for sim) during rollback.
        """
        import os
        legacy = str(os.getenv("NAMESPACE_STREAMS_TRANSPORT") or "true").strip().lower() in {"0", "false", "no", "off"}
        if legacy:
            return "streams" if self.kind == "sim" else "pubsub"
        return "streams"

    def stream_for(self, what: str) -> str:
        """Return the per-mode stream/topic name for an event kind.

        ``what`` is the short event-kind tag, e.g. ``"snapshots"``,
        ``"votes"``, ``"decision_trace"``. For live/oos this returns the
        existing pubsub topic name; for sim it returns the per-run Redis
        Stream name.
        """
        slug = str(what or "").strip().lower()
        if not slug:
            raise ValueError("event-kind 'what' is required")
        if self.kind == "sim":
            return f"stream:{slug}:sim:{self.run_id}"
        if self.kind == "oos":
            return f"market:{slug}:v1:historical"
        return f"market:{slug}:v1"

    # ── redis state-key naming ───────────────────────────────────────────
    def state_key_for(self, key: str) -> str:
        """Prefix a state key (e.g. depth:atm_ce:latest) with the namespace.

        sim runs include the run_id so parallel runs do not collide.
        """
        raw = str(key or "").strip()
        if not raw:
            raise ValueError("state key is required")
        if self.kind == "sim":
            return f"sim:{self.run_id}:{raw}"
        if self.kind == "oos":
            return f"historical:{raw}"
        return f"live:{raw}"

    # ── filesystem run directory ─────────────────────────────────────────
    def run_dir_for(self) -> Path:
        """Filesystem dir where strategy_app writes per-session state."""
        if self.kind == "sim":
            assert self.run_id is not None  # guaranteed by __post_init__
            return _SIM_RUN_DIR_BASE / self.run_id
        if self.kind == "oos":
            return _OOS_RUN_DIR
        return _LIVE_RUN_DIR

    # ── consumer lock (D2: removed) ──────────────────────────────────────
    def lock_key_for(self) -> Optional[str]:
        """Deprecated — always returns ``None``.

        ConsumerLock was removed in D2 (arch/streams-loose-coupling).
        All snapshot consumers now use Redis Streams consumer groups which
        provide exclusive delivery without a separate distributed lock.
        """
        return None


def resolve_namespace(kind: Kind, run_id: Optional[str] = None) -> Namespace:
    """Return the ``Namespace`` for the requested mode.

    For ``kind="sim"`` a non-empty ``run_id`` is required. For ``live`` and
    ``oos`` it is optional and ignored.
    """
    if kind != "sim":
        run_id = None
    return Namespace(kind=kind, run_id=run_id)


__all__ = ["Kind", "Transport", "Namespace", "resolve_namespace"]
