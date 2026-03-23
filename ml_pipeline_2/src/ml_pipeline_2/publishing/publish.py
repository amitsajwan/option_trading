from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _is_repo_root(candidate: Path) -> bool:
    return (candidate / "ml_pipeline_2").exists()


def _validated_repo_root(candidate: Path, *, source: str) -> Path:
    resolved = Path(candidate).resolve()
    if not _is_repo_root(resolved):
        raise ValueError(f"{source} does not point to the option_trading repo root: {resolved}")
    return resolved


def repo_root(explicit_root: Optional[Path] = None) -> Path:
    if explicit_root is not None:
        return _validated_repo_root(Path(explicit_root), source="explicit root")
    env_root = str(os.getenv("MODEL_SWITCH_REPO_ROOT") or os.getenv("ML_PIPELINE_2_REPO_ROOT") or "").strip()
    if env_root:
        return _validated_repo_root(Path(env_root), source="MODEL_SWITCH_REPO_ROOT/ML_PIPELINE_2_REPO_ROOT")
    cwd = Path.cwd().resolve()
    if _is_repo_root(cwd):
        return cwd
    guessed = Path(__file__).resolve().parents[4]
    if _is_repo_root(guessed):
        return guessed
    raise RuntimeError(
        "could not resolve option_trading repo root; set MODEL_SWITCH_REPO_ROOT or "
        "ML_PIPELINE_2_REPO_ROOT, or pass root explicitly"
    )


def published_models_root(*, root: Optional[Path] = None) -> Path:
    return repo_root(root) / "ml_pipeline_2" / "artifacts" / "published_models"
