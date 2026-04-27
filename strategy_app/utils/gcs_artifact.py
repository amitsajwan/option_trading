"""GCS artifact download/cache utility.

Provides transparent gs:// path resolution for model loading.
Downloaded files are cached locally to avoid repeated downloads.
Cache directory: GCS_ARTIFACT_CACHE_DIR env var, or ~/.cache/option_trading_models/
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "option_trading_models"


def _cache_root() -> Path:
    raw = str(os.getenv("GCS_ARTIFACT_CACHE_DIR") or "").strip()
    return Path(raw) if raw else _DEFAULT_CACHE_ROOT


def _cache_local_path(gcs_url: str) -> Path:
    key = hashlib.sha256(gcs_url.encode()).hexdigest()[:12]
    filename = gcs_url.rstrip("/").rsplit("/", 1)[-1] or "artifact"
    slug = (
        gcs_url.replace("gs://", "")
        .replace("/", "_")
        .replace(".", "-")[:64]
    )
    return _cache_root() / f"{slug}_{key}" / filename


def _parse_gcs_url(gcs_url: str) -> tuple[str, str]:
    if not gcs_url.startswith("gs://"):
        raise ValueError(f"not a GCS URL: {gcs_url!r}")
    rest = gcs_url[5:]
    bucket, _, blob = rest.partition("/")
    return bucket, blob


def _get_storage_client() -> Any:
    try:
        from google.cloud import storage  # type: ignore
        return storage.Client()
    except ImportError:
        raise ImportError(
            "google-cloud-storage is required for GCS model loading. "
            "Run: pip install google-cloud-storage"
        )


def is_gcs_path(s: Any) -> bool:
    """Return True if s is a gs:// URL."""
    return str(s or "").strip().startswith("gs://")


def download_gcs_file(gcs_url: str, *, force: bool = False) -> Path:
    """Download a single GCS object to local cache; return local Path."""
    local = _cache_local_path(gcs_url)
    if local.exists() and not force:
        logger.debug("GCS cache hit: %s", gcs_url)
        return local
    bucket_name, blob_path = _parse_gcs_url(gcs_url)
    local.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading GCS artifact: %s", gcs_url)
    client = _get_storage_client()
    client.bucket(bucket_name).blob(blob_path).download_to_filename(str(local))
    logger.info("Cached → %s", local)
    return local


def fetch_gcs_json(gcs_url: str, *, force: bool = False) -> Optional[dict[str, Any]]:
    """Download a GCS JSON file and return parsed dict, or None on any failure."""
    try:
        local = download_gcs_file(gcs_url, force=force)
        return json.loads(local.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Could not fetch GCS JSON %s: %s", gcs_url, exc)
        return None


def resolve_artifact_path(path: str, *, force: bool = False) -> str:
    """If path is gs://, download to local cache and return local path. Otherwise pass through."""
    if not is_gcs_path(path):
        return path
    return str(download_gcs_file(path, force=force))
