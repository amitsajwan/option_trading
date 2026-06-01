"""ExtendedRegimePlugin — production default RegimePlugin using all 10 regime labels.

Uses the updated RegimeClassifier which includes the Phase 3 labels:
CHOP, BREAKOUT, PANIC, DEAD_MARKET.

Swap between legacy and extended classifier via env var:
    REGIME_PLUGIN=legacy           → RegimeClassifierAdapter (6-label legacy)
    REGIME_PLUGIN=extended_v1      → ExtendedRegimePlugin (10-label, default)
"""
from __future__ import annotations

import os
from typing import Any, Optional

from ..brain.plugin import RegimeDecisionResult, RegimePlugin
from .regime import RegimeClassifier
from .regime_plugin_adapter import RegimeClassifierAdapter
from .snapshot_accessor import SnapshotAccessor


class ExtendedRegimePlugin(RegimePlugin):
    """Wraps RegimeClassifier with all 10 regime labels as a RegimePlugin.

    Identical in structure to RegimeClassifierAdapter but explicitly named
    as the Phase 3 production plugin so metrics and logs can distinguish it
    from the legacy 6-label adapter.
    """

    @property
    def plugin_id(self) -> str:
        return "extended_regime_v1"

    @property
    def plugin_version(self) -> str:
        return "1.0"

    def __init__(self, *, classifier: Optional[RegimeClassifier] = None) -> None:
        self._classifier = classifier if classifier is not None else RegimeClassifier()

    def configure(self, config: Optional[dict[str, object]]) -> None:
        self._classifier.configure(config)

    def classify(
        self,
        snapshot: dict[str, Any],
        context: dict[str, Any],
    ) -> RegimeDecisionResult:
        snap = SnapshotAccessor(snapshot)
        signal = self._classifier.classify(snap)
        return RegimeDecisionResult(
            regime=signal.regime.value,
            confidence=signal.confidence,
            evidence={**signal.evidence, "reason": signal.reason},
            plugin_id=self.plugin_id,
            plugin_version=self.plugin_version,
        )


def resolve_regime_plugin(plugin_name: Optional[str] = None) -> RegimePlugin:
    """Factory that returns the RegimePlugin for the given name (or env var).

    Resolution order:
      1. ``plugin_name`` argument
      2. ``REGIME_PLUGIN`` env var
      3. Default: ExtendedRegimePlugin

    Valid names: ``'extended_v1'`` (default), ``'legacy'``.
    """
    name = str(plugin_name or os.getenv("REGIME_PLUGIN") or "extended_v1").strip().lower()
    if name == "legacy":
        return RegimeClassifierAdapter()
    return ExtendedRegimePlugin()


__all__ = ["ExtendedRegimePlugin", "resolve_regime_plugin"]
