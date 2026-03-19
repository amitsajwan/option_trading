from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def repo_root(explicit_root: Optional[Path] = None) -> Path:
    if explicit_root is not None:
        return Path(explicit_root).resolve()
    env_root = str(os.getenv("MODEL_SWITCH_REPO_ROOT") or os.getenv("ML_PIPELINE_2_REPO_ROOT") or "").strip()
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "ml_pipeline_2" / "artifacts").exists():
        return cwd
    guessed = Path(__file__).resolve().parents[4]
    return guessed if (guessed / "ml_pipeline_2").exists() else cwd


def published_models_root(*, root: Optional[Path] = None) -> Path:
    return repo_root(root) / "ml_pipeline_2" / "artifacts" / "published_models"
