from __future__ import annotations

from .recipes import FIXED_RECIPE_CATALOG_ID, get_recipe_catalog, recipe_catalog_ids
from .runtime_contract import (
    STAGED_RUNTIME_BUNDLE_KIND,
    STAGED_RUNTIME_POLICY_KIND,
    load_staged_runtime_policy,
    validate_recipe_catalog_payload,
)

__all__ = [
    "FIXED_RECIPE_CATALOG_ID",
    "STAGED_RUNTIME_BUNDLE_KIND",
    "STAGED_RUNTIME_POLICY_KIND",
    "assess_staged_release_candidate",
    "get_recipe_catalog",
    "load_staged_runtime_policy",
    "publish_staged_run",
    "recipe_catalog_ids",
    "release_staged_run",
    "run_staged_research",
    "validate_recipe_catalog_payload",
]


def __getattr__(name: str):
    if name == "run_staged_research":
        from .pipeline import run_staged_research

        return run_staged_research
    if name in {"assess_staged_release_candidate", "publish_staged_run", "release_staged_run"}:
        from .publish import assess_staged_release_candidate, publish_staged_run, release_staged_run

        return {
            "assess_staged_release_candidate": assess_staged_release_candidate,
            "publish_staged_run": publish_staged_run,
            "release_staged_run": release_staged_run,
        }[name]
    raise AttributeError(name)
