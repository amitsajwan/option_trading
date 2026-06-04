"""Lightweight per-phase wall-time profiling for the engine's evaluate() loop.

Gated by ``STRATEGY_EVAL_TIMING`` (default off) so it is effectively
zero-overhead in normal/live runs.  Enable it during SIM/replay to see where
the per-bar budget actually goes — regime classification, ML vote collection,
shadow scoring, the Redis depth read, and the entry gate cascade — before
deciding whether any latency work (e.g. parallelism, memoisation) is warranted.

The numbers are wall-time in milliseconds, aggregated per session and logged at
``on_session_end``.  This measures *where* time goes, not absolute throughput;
treat it as a relative breakdown across phases of the same run.
"""
from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Iterator


class PhaseTimer:
    """Accumulates per-phase wall-time across many ``evaluate()`` calls.

    When ``enabled`` is False every method is a cheap no-op, so call sites can
    stay unconditionally wrapped in ``with timer.measure(...)``.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._total_ms: dict[str, float] = {}
        self._count: dict[str, int] = {}
        self._max_ms: dict[str, float] = {}
        self._bars = 0

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = perf_counter()
        try:
            yield
        finally:
            dt = (perf_counter() - start) * 1000.0
            self._total_ms[phase] = self._total_ms.get(phase, 0.0) + dt
            self._count[phase] = self._count.get(phase, 0) + 1
            if dt > self._max_ms.get(phase, 0.0):
                self._max_ms[phase] = dt

    def mark_bar(self) -> None:
        """Count one full ``evaluate()`` call (one snapshot/bar)."""
        if self.enabled:
            self._bars += 1

    @property
    def bars(self) -> int:
        return self._bars

    def summary(self) -> dict[str, dict[str, float]]:
        """Structured per-phase stats (for programmatic/test consumption)."""
        out: dict[str, dict[str, float]] = {}
        for phase, total in self._total_ms.items():
            count = self._count.get(phase, 0)
            out[phase] = {
                "total_ms": round(total, 2),
                "count": float(count),
                "mean_ms": round(total / count, 4) if count else 0.0,
                "max_ms": round(self._max_ms.get(phase, 0.0), 3),
                "per_bar_ms": round(total / self._bars, 4) if self._bars else 0.0,
            }
        return out

    def format_summary(self) -> str:
        """One-line human-readable breakdown, sorted by total time desc.

        Percentages are share of the ``total`` phase (the whole evaluate body),
        so they show how each phase contributes to the per-bar cost.
        """
        if not self._bars:
            return "no bars measured"
        total_phase = self._total_ms.get("total")
        parts = []
        ordered = sorted(self._total_ms.items(), key=lambda kv: kv[1], reverse=True)
        for phase, total in ordered:
            per_bar = total / self._bars
            pct = ""
            if total_phase and total_phase > 0 and phase != "total":
                pct = f",{100.0 * total / total_phase:.0f}%"
            mx = self._max_ms.get(phase, 0.0)
            parts.append(f"{phase}={per_bar:.3f}ms/bar(max={mx:.1f}{pct})")
        return f"bars={self._bars} " + " ".join(parts)

    def reset(self) -> None:
        self._total_ms.clear()
        self._count.clear()
        self._max_ms.clear()
        self._bars = 0
