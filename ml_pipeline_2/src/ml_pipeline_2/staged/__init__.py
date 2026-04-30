from __future__ import annotations

from .recipes import FIXED_RECIPE_CATALOG_ID, get_recipe_catalog, recipe_catalog_ids
from .runtime_contract import (
    STAGED_RUNTIME_BUNDLE_KIND,
    STAGED_RUNTIME_POLICY_KIND,
    load_staged_runtime_policy,
    validate_recipe_catalog_payload,
)
from .scenario_runner import (
    build_manifest,
    scenario_matrix,
    validate_manifest,
    write_manifest,
)
from .config_diff import diff_manifests, print_diff
from .results_analyzer import (
    RunComparison,
    RunMetrics,
    compare_runs,
    extract_summary_metrics,
)

__all__ = [
    "FIXED_RECIPE_CATALOG_ID",
    "STAGED_RUNTIME_BUNDLE_KIND",
    "STAGED_RUNTIME_POLICY_KIND",
    "assess_staged_release_candidate",
    "build_manifest",
    "compare_runs",
    "diff_manifests",
    "extract_summary_metrics",
    "get_recipe_catalog",
    "load_staged_runtime_policy",
    "print_diff",
    "publish_staged_run",
    "recipe_catalog_ids",
    "release_staged_run",
    "RunComparison",
    "RunMetrics",
    "run_staged_research",
    "scenario_matrix",
    "validate_manifest",
    "validate_recipe_catalog_payload",
    "write_manifest",
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
