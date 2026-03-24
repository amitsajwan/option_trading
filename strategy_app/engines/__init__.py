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
]


def __getattr__(name: str) -> Any:
    if name == "DeterministicRuleEngine":
        return import_module(".deterministic_rule_engine", __name__).DeterministicRuleEngine
    if name == "PureMLEngine":
        return import_module(".pure_ml_engine", __name__).PureMLEngine
    if name in {"EntryPolicy", "EntryPolicyDecision", "LongOptionEntryPolicy", "PolicyConfig"}:
        module = import_module(".entry_policy", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
