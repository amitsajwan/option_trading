#!/usr/bin/env python3
"""Generate and manage GCP runtime release manifest artifacts."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_KIND = "gcp_runtime_release_manifest_v1"
POINTER_KIND = "gcp_runtime_release_pointer_v1"
CURRENT_RELEASE_DIR = Path(".run/gcp_release")
CURRENT_MANIFEST_NAME = "current_runtime_release.json"
CURRENT_POINTER_NAME = "current_runtime_release_pointer.json"
CURRENT_RUNTIME_ENV_NAME = "current_ml_pure_runtime.env"


class ReleaseManifestError(ValueError):
    """Raised when a runtime release manifest cannot be generated."""


@dataclass(frozen=True)
class RuntimeReleaseArtifacts:
    manifest_path: Path
    current_manifest_path: Path
    current_pointer_path: Path
    current_runtime_env_path: Path
    manifest: dict[str, Any]
    pointer: dict[str, Any]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReleaseManifestError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _repo_relative(path: Path, *, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def _current_release_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    current_dir = repo_root / CURRENT_RELEASE_DIR
    return (
        current_dir / CURRENT_MANIFEST_NAME,
        current_dir / CURRENT_POINTER_NAME,
        current_dir / CURRENT_RUNTIME_ENV_NAME,
    )


def build_runtime_release_manifest(
    *,
    training_release_path: Path,
    repo_root: Path,
    app_image_tag: str,
    runtime_guard_path: str,
    runtime_config_bucket_url: str | None = None,
) -> RuntimeReleaseArtifacts:
    payload = _load_json(training_release_path)
    release_status = str(payload.get("release_status") or "").strip().lower()
    if release_status != "published":
        raise ReleaseManifestError(
            f"training release is not published: {training_release_path} release_status={release_status or 'missing'}"
        )

    publish = dict(payload.get("publish") or {})
    active_group_paths = dict(publish.get("active_group_paths") or {})
    run_id = str(publish.get("run_id") or payload.get("run_id") or "").strip()
    model_group = str(publish.get("model_group") or "").strip()
    profile_id = str(publish.get("profile_id") or "").strip()
    runtime_env_raw = str(((payload.get("paths") or {}).get("runtime_env") or "")).strip()
    release_summary_raw = str(((payload.get("paths") or {}).get("release_summary") or "")).strip()
    threshold_report = str(active_group_paths.get("threshold_report") or "").strip()
    training_summary = str(active_group_paths.get("training_report") or "").strip()

    if not run_id:
        raise ReleaseManifestError("training release missing publish.run_id")
    if not model_group:
        raise ReleaseManifestError("training release missing publish.model_group")
    if not runtime_env_raw:
        raise ReleaseManifestError("training release missing paths.runtime_env")
    if not threshold_report:
        raise ReleaseManifestError("training release missing publish.active_group_paths.threshold_report")
    if not training_summary:
        raise ReleaseManifestError("training release missing publish.active_group_paths.training_report")
    if not str(app_image_tag or "").strip():
        raise ReleaseManifestError("app_image_tag is required")
    if not str(runtime_guard_path or "").strip():
        raise ReleaseManifestError("runtime_guard_path is required")

    runtime_env_path = Path(runtime_env_raw)
    release_summary_path = Path(release_summary_raw) if release_summary_raw else None
    manifest_path = runtime_env_path.parent / "runtime_release_manifest.json"
    current_manifest_path, current_pointer_path, current_runtime_env_path = _current_release_paths(repo_root)

    manifest = {
        "kind": MANIFEST_KIND,
        "created_at_utc": str(payload.get("created_at_utc") or publish.get("created_at_utc") or ""),
        "release_status": "published",
        "strategy_engine": "ml_pure",
        "run_id": run_id,
        "model_group": model_group,
        "profile_id": profile_id or None,
        "app_image_tag": str(app_image_tag).strip(),
        "runtime_guard_path": str(runtime_guard_path).strip(),
        "threshold_report": threshold_report,
        "training_summary": training_summary,
        "runtime_env_path": _repo_relative(runtime_env_path, repo_root=repo_root),
        "release_summary_path": (
            _repo_relative(release_summary_path, repo_root=repo_root) if release_summary_path is not None else None
        ),
        "source_training_release_json": _repo_relative(training_release_path, repo_root=repo_root),
        "runtime_config_bucket_url": str(runtime_config_bucket_url or "").strip() or None,
    }

    _write_json(manifest_path, manifest)
    current_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(current_manifest_path, manifest)
    current_runtime_env_path.write_text(runtime_env_path.read_text(encoding="utf-8"), encoding="utf-8")

    pointer = {
        "kind": POINTER_KIND,
        "updated_at_utc": manifest["created_at_utc"],
        "release_status": "published",
        "run_id": run_id,
        "model_group": model_group,
        "profile_id": profile_id or None,
        "current_manifest_path": _repo_relative(current_manifest_path, repo_root=repo_root),
        "current_runtime_env_path": _repo_relative(current_runtime_env_path, repo_root=repo_root),
        "source_manifest_path": _repo_relative(manifest_path, repo_root=repo_root),
        "runtime_config_bucket_url": str(runtime_config_bucket_url or "").strip() or None,
    }
    _write_json(current_pointer_path, pointer)

    return RuntimeReleaseArtifacts(
        manifest_path=manifest_path,
        current_manifest_path=current_manifest_path,
        current_pointer_path=current_pointer_path,
        current_runtime_env_path=current_runtime_env_path,
        manifest=manifest,
        pointer=pointer,
    )


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write runtime release manifest artifacts")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--training-release-json", required=True)
    parser.add_argument("--app-image-tag", required=True)
    parser.add_argument("--runtime-guard-path", required=True)
    parser.add_argument("--runtime-config-bucket-url", default="")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    try:
        artifacts = build_runtime_release_manifest(
            training_release_path=Path(args.training_release_json).resolve(),
            repo_root=repo_root,
            app_image_tag=str(args.app_image_tag),
            runtime_guard_path=str(args.runtime_guard_path),
            runtime_config_bucket_url=str(args.runtime_config_bucket_url or ""),
        )
    except ReleaseManifestError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "training_release_json": str(Path(args.training_release_json).resolve()),
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "manifest_path": str(artifacts.manifest_path.resolve()),
                "current_manifest_path": str(artifacts.current_manifest_path.resolve()),
                "current_pointer_path": str(artifacts.current_pointer_path.resolve()),
                "current_runtime_env_path": str(artifacts.current_runtime_env_path.resolve()),
                "run_id": artifacts.manifest["run_id"],
                "model_group": artifacts.manifest["model_group"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
