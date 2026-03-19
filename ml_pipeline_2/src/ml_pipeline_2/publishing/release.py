from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .publish import published_models_root, repo_root


def sync_published_model_group_to_gcs(
    *,
    model_bucket_url: str,
    model_group: str,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    bucket_url = str(model_bucket_url or "").strip().rstrip("/")
    group = str(model_group or "").strip().strip("/\\")
    if not bucket_url:
        raise ValueError("model_bucket_url must be non-empty")
    if not group:
        raise ValueError("model_group must be non-empty")
    publish_root = repo_root(root)
    source_path = (published_models_root(root=publish_root) / Path(group)).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"published model group not found: {source_path}")
    target_url = f"{bucket_url}/{Path(group).as_posix()}"
    cmd = ["gcloud", "storage", "rsync", str(source_path), target_url, "--recursive"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "gcloud storage rsync failed: "
            f"exit={result.returncode} stderr={result.stderr.strip() or result.stdout.strip()}"
        )
    return {
        "status": "completed",
        "command": cmd,
        "source_path": str(source_path),
        "target_url": target_url,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
