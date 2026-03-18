from __future__ import annotations

from .publish import publish_recovery_run
from .release import assess_recovery_release_candidate, release_recovery_run, sync_published_model_group_to_gcs
from .resolver import resolve_ml_pure_artifacts, validate_switch_strict

__all__ = [
    "assess_recovery_release_candidate",
    "assess_staged_release_candidate",
    "publish_recovery_run",
    "publish_staged_run",
    "release_recovery_run",
    "release_staged_run",
    "resolve_ml_pure_artifacts",
    "sync_published_model_group_to_gcs",
    "validate_switch_strict",
]


def __getattr__(name: str):
    if name in {"assess_staged_release_candidate", "publish_staged_run", "release_staged_run"}:
        from ..staged.publish import (
            assess_staged_release_candidate,
            publish_staged_run,
            release_staged_run,
        )

        registry = {
            "assess_staged_release_candidate": assess_staged_release_candidate,
            "publish_staged_run": publish_staged_run,
            "release_staged_run": release_staged_run,
        }
        return registry[name]
    raise AttributeError(name)
