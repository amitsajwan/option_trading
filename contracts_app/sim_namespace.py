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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

Kind = Literal["live", "oos", "sim"]
Transport = Literal["pubsub", "streams"]

_VALID_KINDS: tuple[Kind, ...] = ("live", "oos", "sim")

# ── Instrument axis (orthogonal to kind) ─────────────────────────────────────
# Instrument is the SECOND dimension of the namespace. It composes with kind so
# that {instrument} x {kind} each get an isolated naming surface, all generated
# here. Design invariant: the PRIMARY instrument contributes an EMPTY segment to
# every name, so its output is byte-identical to the pre-instrument-axis era.
# This keeps all existing live/sim collections, topics, run-dirs, locks, and
# replays working with zero migration; secondary instruments (NIFTY, ...) are
# purely additive. See tests/test_sim_namespace.py::TestInstrumentParity.
PRIMARY_INSTRUMENT = "BANKNIFTY"


def normalize_instrument(instrument: Optional[str]) -> str:
    """Canonical UPPER-CASE instrument name; empty/None -> primary."""
    raw = str(instrument or "").strip().upper()
    return raw or PRIMARY_INSTRUMENT


def _instrument_slug(instrument: str) -> str:
    """Lower-case slug inserted into names. Primary -> '' (no segment)."""
    canon = normalize_instrument(instrument)
    return "" if canon == PRIMARY_INSTRUMENT else canon.lower()


def current_instrument() -> str:
    """The instrument this process is scoped to, from STRATEGY_INSTRUMENT env.

    One env var scopes a whole container's namespace (collections, topics,
    run-dir, lock). Unset/empty -> primary (BANKNIFTY), preserving legacy
    single-instrument behavior.
    """
    return normalize_instrument(os.getenv("STRATEGY_INSTRUMENT"))

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
        # Raw live-feed collections (persistence_app.mongo_sink). Namespaced so
        # parallel instrument stacks don't share a raw tick/option/depth store.
        "live_ticks",
        "live_options_chain",
        "live_depth",
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
    """Resolved naming surface for one (instrument, mode) pair.

    ``instrument`` is the orthogonal second axis: the PRIMARY instrument
    (BANKNIFTY) contributes no segment, so every name is identical to the
    pre-instrument-axis era; secondary instruments insert a lower-case slug.
    """

    kind: Kind
    run_id: Optional[str] = None
    instrument: str = PRIMARY_INSTRUMENT

    def __post_init__(self) -> None:  # pragma: no cover - trivial
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"unknown kind={self.kind!r}; expected one of {_VALID_KINDS}"
            )
        if self.kind == "sim" and not self.run_id:
            raise ValueError("sim namespace requires a non-empty run_id")
        # Normalize the instrument in place (frozen dataclass -> object.__setattr__).
        object.__setattr__(self, "instrument", normalize_instrument(self.instrument))

    # ── instrument segment helper ────────────────────────────────────────
    @property
    def _instr_seg(self) -> str:
        """Lower-case instrument slug, or '' for the primary instrument."""
        return _instrument_slug(self.instrument)

    # ── collection naming ────────────────────────────────────────────────
    def collection_for(self, base: str) -> str:
        """Return the Mongo collection name for ``base`` in this namespace.

        ``base`` not in the namespaced-bases set is returned unchanged —
        this lets shared collections (eg. ``strategy_eval_runs``) pass through
        without callers needing to special-case them.

        Naming: ``{base}[_{instrument}][_{kind}]`` — instrument and kind
        segments are each omitted for primary/live, preserving legacy names.
        """
        key = str(base or "").strip()
        if not key:
            raise ValueError("collection base name is required")
        if key not in _NAMESPACED_BASES:
            return key
        instr = self._instr_seg
        name = f"{key}_{instr}" if instr else key
        if self.kind == "oos":
            return f"{name}_historical"
        if self.kind == "sim":
            return f"{name}_sim"
        return name

    # ── transport / topic / stream naming ────────────────────────────────
    def transport(self) -> Transport:
        """``streams`` for sim, ``pubsub`` for live and oos."""
        return "streams" if self.kind == "sim" else "pubsub"

    def stream_for(self, what: str) -> str:
        """Return the per-mode stream/topic name for an event kind.

        ``what`` is the short event-kind tag, e.g. ``"snapshots"``,
        ``"votes"``, ``"decision_trace"``. For live/oos this returns the
        existing pubsub topic name; for sim it returns the per-run Redis
        Stream name. The instrument slug is inserted right after the prefix
        for secondary instruments (``market:nifty:snapshots:v1``).
        """
        slug = str(what or "").strip().lower()
        if not slug:
            raise ValueError("event-kind 'what' is required")
        instr = self._instr_seg
        if self.kind == "sim":
            mid = f"{slug}:{instr}" if instr else slug
            return f"stream:{mid}:sim:{self.run_id}"
        prefix = f"market:{instr}" if instr else "market"
        if self.kind == "oos":
            return f"{prefix}:{slug}:v1:historical"
        return f"{prefix}:{slug}:v1"

    # ── redis state-key naming ───────────────────────────────────────────
    def state_key_for(self, key: str) -> str:
        """Prefix a state key (e.g. depth:atm_ce:latest) with the namespace.

        sim runs include the run_id so parallel runs do not collide; the
        instrument slug is inserted for secondary instruments.
        """
        raw = str(key or "").strip()
        if not raw:
            raise ValueError("state key is required")
        instr = self._instr_seg
        if self.kind == "sim":
            head = f"sim:{instr}:{self.run_id}" if instr else f"sim:{self.run_id}"
            return f"{head}:{raw}"
        base = "historical" if self.kind == "oos" else "live"
        head = f"{base}:{instr}" if instr else base
        return f"{head}:{raw}"

    # ── filesystem run directory ─────────────────────────────────────────
    def run_dir_for(self) -> Path:
        """Filesystem dir where strategy_app writes per-session state.

        Secondary instruments get a parallel dir (``strategy_app_nifty``)
        so two instrument stacks never share session state.
        """
        instr = self._instr_seg
        suffix = f"_{instr}" if instr else ""
        if self.kind == "sim":
            assert self.run_id is not None  # guaranteed by __post_init__
            base = Path(f"{_SIM_RUN_DIR_BASE}{suffix}")
            return base / self.run_id
        if self.kind == "oos":
            return Path(f"{_OOS_RUN_DIR}{suffix}")
        return Path(f"{_LIVE_RUN_DIR}{suffix}")

    # ── consumer lock ────────────────────────────────────────────────────
    def lock_key_for(self) -> Optional[str]:
        """Redis consumer-lock key. ``None`` for sim — by design, sim runs
        use ephemeral consumer containers + Redis Streams consumer groups,
        so locking is unnecessary and was actively harmful in early designs.
        """
        if self.kind == "sim":
            return None
        # The lock key references this namespace's own snapshot stream so two
        # instrument stacks (each consuming a different snapshot topic) take
        # independent locks.
        snapshot_stream = self.stream_for("snapshot")
        instr = self._instr_seg
        if self.kind == "oos":
            app = f"strategy_app_{instr}_historical" if instr else "strategy_app_historical"
            return f"{app}:consumer_lock:{snapshot_stream}"
        app = f"strategy_app_{instr}" if instr else "strategy_app"
        return f"{app}:consumer_lock:{snapshot_stream}"


def resolve_namespace(
    kind: Kind,
    run_id: Optional[str] = None,
    instrument: Optional[str] = None,
) -> Namespace:
    """Return the ``Namespace`` for the requested (instrument, mode) pair.

    For ``kind="sim"`` a non-empty ``run_id`` is required. For ``live`` and
    ``oos`` it is optional and ignored. ``instrument`` defaults to the primary
    instrument (BANKNIFTY) when None/empty, which yields legacy names.
    """
    if kind != "sim":
        run_id = None
    return Namespace(kind=kind, run_id=run_id, instrument=normalize_instrument(instrument))


__all__ = [
    "Kind",
    "Transport",
    "Namespace",
    "resolve_namespace",
    "PRIMARY_INSTRUMENT",
    "normalize_instrument",
    "current_instrument",
]
