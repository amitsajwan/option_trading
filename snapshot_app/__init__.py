from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "project_stage1_entry_view",
    "project_stage2_direction_view",
    "project_stage3_recipe_view",
    "project_stage_views",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = import_module(".core.stage_views", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
