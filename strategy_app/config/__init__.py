"""Single-source strategy config: one grouped YAML, one registry, one loader.

See ``docs/strategy_platform/CONFIG_CONSOLIDATION_PLAN.md``.
"""
from .loader import apply_to_environ, load_yaml, resolve
from .registry import (
    BY_ENV,
    BY_YAML,
    OPS_ENV_KEYS,
    REGISTRY,
    SAFE_OVERRIDE_KEYS,
    ConfigKey,
)
from .typed import value, view

__all__ = [
    "apply_to_environ",
    "load_yaml",
    "resolve",
    "REGISTRY",
    "BY_ENV",
    "BY_YAML",
    "OPS_ENV_KEYS",
    "SAFE_OVERRIDE_KEYS",
    "ConfigKey",
    "value",
    "view",
]
