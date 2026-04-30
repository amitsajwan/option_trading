"""Strategy engine implementations."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DeterministicRuleEngine",
    "EntryPolicy",
    "EntryPolicyDecision",
    "LongOptionEntryPolicy",
    "PureMLEngine",
    "PolicyConfig",
    "VelocityEnhancedRegimeClassifier",
    "VelocityEnhancedEntryPolicy",
]


def __getattr__(name: str) -> Any:
    if name == "DeterministicRuleEngine":
        return import_module(".deterministic_rule_engine", __name__).DeterministicRuleEngine
    if name == "PureMLEngine":
        return import_module(".pure_ml_engine", __name__).PureMLEngine
    if name in {"EntryPolicy", "EntryPolicyDecision", "LongOptionEntryPolicy", "PolicyConfig"}:
        module = import_module(".entry_policy", __name__)
        return getattr(module, name)
    if name == "VelocityEnhancedRegimeClassifier":
        return import_module(".velocity_regime_classifier", __name__).VelocityEnhancedRegimeClassifier
    if name == "VelocityEnhancedEntryPolicy":
        return import_module(".velocity_entry_policy", __name__).VelocityEnhancedEntryPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
