"""Adapter that exposes the existing RegimeClassifier through the RegimePlugin interface.

This is the default plugin injected into RegimeDecisionConsumer.  It preserves
all existing classification behaviour unchanged — the adapter is a pure wrapper
that only bridges the interface gap.
"""
from __future__ import annotations

from typing import Any, Optional

from ..brain.plugin import RegimeDecisionResult, RegimePlugin
from .regime import RegimeClassifier
from .snapshot_accessor import SnapshotAccessor


class RegimeClassifierAdapter(RegimePlugin):
    """Wraps :class:`RegimeClassifier` as a :class:`RegimePlugin`.

    plugin_id is fixed at ``'regime_classifier_v1'``.  Configure threshold
    overrides (e.g. from run metadata) via :meth:`configure`.

    Usage::

        adapter = RegimeClassifierAdapter()
        result = adapter.classify(snapshot_payload, context={})
    """

    @property
    def plugin_id(self) -> str:
        return "regime_classifier_v1"

    @property
    def plugin_version(self) -> str:
        return "1.0"

    def __init__(self, *, classifier: Optional[RegimeClassifier] = None) -> None:
        self._classifier = classifier if classifier is not None else RegimeClassifier()

    def configure(self, config: Optional[dict[str, object]]) -> None:
        """Forward threshold overrides to the underlying :class:`RegimeClassifier`."""
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


__all__ = ["RegimeClassifierAdapter"]
