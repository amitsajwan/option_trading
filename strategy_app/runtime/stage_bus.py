"""StageBus — EventBus decorator that stamps run context on every published event.

Each stage consumer constructs one ``StageBus`` at startup (via
:meth:`StageBus.from_env` or directly) and calls
:meth:`publish_decision` instead of ``bus.publish`` directly.  This ensures
``run_id``, ``parity_mode``, ``plugin_id``, and ``plugin_version`` are
consistent across all events emitted by a single consumer process without
each call site having to pass them explicitly.

``StageBus`` is NOT a new ``EventBus`` implementation — it delegates all
Redis operations to the injected bus and adds only context-stamping logic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from contracts_app.event_bus import EventBus
from contracts_app.parity_mode import ParityMode, infer_parity_mode


@dataclass
class StageBusConfig:
    """Per-consumer context fields stamped onto every decision event."""

    run_id: str
    parity_mode: ParityMode
    plugin_id: str = ""
    plugin_version: str = ""


class StageBus:
    """Context-stamping wrapper around :class:`EventBus`.

    Stamps ``run_id``, ``parity_mode``, ``plugin_id``, and ``plugin_version``
    onto every event dict before delegating to the underlying bus.  Existing
    values in the event dict are preserved (``setdefault`` semantics).
    """

    def __init__(self, bus: EventBus, config: StageBusConfig) -> None:
        self._bus = bus
        self._config = config

    @classmethod
    def from_env(
        cls,
        bus: EventBus,
        *,
        plugin_id: str = "",
        plugin_version: str = "",
    ) -> StageBus:
        """Build a :class:`StageBus` reading ``run_id`` and ``source_mode`` from env vars.

        Expected env vars::

            STRATEGY_RUN_ID      — the current run identifier
            STRATEGY_SOURCE_MODE — one of: live, oos, sim  (default: live)
        """
        run_id = str(os.getenv("STRATEGY_RUN_ID") or "").strip()
        source_mode = str(os.getenv("STRATEGY_SOURCE_MODE") or "live").strip()
        parity = infer_parity_mode(source_mode)
        return cls(bus, StageBusConfig(
            run_id=run_id,
            parity_mode=parity,
            plugin_id=str(plugin_id or ""),
            plugin_version=str(plugin_version or ""),
        ))

    # ── context mutation ───────────────────────────────────────────────────

    def set_plugin(self, plugin_id: str, plugin_version: str) -> None:
        """Override plugin identity — call before starting the consume loop."""
        self._config = StageBusConfig(
            run_id=self._config.run_id,
            parity_mode=self._config.parity_mode,
            plugin_id=str(plugin_id or ""),
            plugin_version=str(plugin_version or ""),
        )

    # ── context accessors ──────────────────────────────────────────────────

    @property
    def run_id(self) -> str:
        return self._config.run_id

    @property
    def parity_mode(self) -> ParityMode:
        return self._config.parity_mode

    @property
    def plugin_id(self) -> str:
        return self._config.plugin_id

    @property
    def plugin_version(self) -> str:
        return self._config.plugin_version

    # ── publish ────────────────────────────────────────────────────────────

    def publish_decision(self, stream: str, event: dict[str, Any]) -> None:
        """Stamp context fields onto *event* then delegate to the underlying bus.

        Uses ``setdefault`` so event-level values (e.g. plugin_id from a
        specific plugin instance) are not overwritten by the bus-level defaults.

        Raises ``ValueError`` if ``plugin_id`` or ``plugin_version`` are empty
        after stamping — every decision event must identify the plugin that made it.
        """
        stamped = dict(event)
        stamped.setdefault("run_id", self._config.run_id)
        stamped.setdefault("parity_mode", self._config.parity_mode.value)
        stamped.setdefault("plugin_id", self._config.plugin_id)
        stamped.setdefault("plugin_version", self._config.plugin_version)

        if not str(stamped.get("plugin_id") or "").strip():
            raise ValueError(
                f"publish_decision to {stream!r}: plugin_id must be non-empty. "
                "Call bus.set_plugin(plugin_id, plugin_version) before starting the consume loop."
            )
        if not str(stamped.get("plugin_version") or "").strip():
            raise ValueError(
                f"publish_decision to {stream!r}: plugin_version must be non-empty."
            )
        if not str(stamped.get("parity_mode") or "").strip():
            raise ValueError(
                f"publish_decision to {stream!r}: parity_mode must be non-empty."
            )

        self._bus.publish(stream, stamped)

    # ── EventBus pass-throughs ─────────────────────────────────────────────

    def publish(self, stream: str, event: dict[str, Any]) -> None:
        """Direct publish without context stamping (for non-decision events)."""
        self._bus.publish(stream, event)

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 2000,
        stream_id: str = ">",
    ) -> list[tuple[str, dict[str, Any]]]:
        return self._bus.consume(stream, group, consumer, count=count, block_ms=block_ms, stream_id=stream_id)

    def acknowledge(self, stream: str, group: str, message_id: str) -> None:
        self._bus.acknowledge(stream, group, message_id)

    def ensure_group(self, stream: str, group: str) -> None:
        self._bus.ensure_group(stream, group)

    def ping(self) -> bool:
        return self._bus.ping()


__all__ = ["StageBus", "StageBusConfig"]
