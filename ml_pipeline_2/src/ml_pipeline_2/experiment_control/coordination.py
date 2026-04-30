from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional


RunReuseMode = Literal["fail_if_exists", "resume", "restart"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CoordinationError(RuntimeError):
    pass


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _has_contents(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _archive_existing_root(path: Path) -> Path:
    archived = path.with_name(f"{path.name}.abandoned_{_timestamp_suffix()}")
    path.rename(archived)
    return archived


def _clear_lock_best_effort(lock_path: Path) -> bool:
    if not lock_path.exists():
        return True
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        try:
            released_path = lock_path.with_name(f"{lock_path.name}.released_{_timestamp_suffix()}")
            lock_path.replace(released_path)
            return True
        except Exception:
            return False


def prepare_output_root(
    output_root: Path,
    *,
    reuse_mode: RunReuseMode,
    summary_filename: str,
    entity_name: str,
    lock_filename: str,
) -> Dict[str, Any]:
    root = Path(output_root).resolve()
    summary_path = root / summary_filename
    lock_path = root / lock_filename
    existing_summary = _load_json(summary_path) if summary_path.exists() else None

    if reuse_mode == "fail_if_exists":
        if _has_contents(root):
            raise CoordinationError(
                f"{entity_name} already exists and is non-empty: {root}. "
                "Use a fresh output root or choose resume/restart explicitly."
            )
        root.mkdir(parents=True, exist_ok=True)
        return {
            "output_root": root,
            "archived_root": None,
            "existing_summary": None,
        }

    if reuse_mode == "restart":
        archived_root = None
        if _has_contents(root):
            if lock_path.exists():
                raise CoordinationError(
                    f"{entity_name} appears active and cannot be restarted while locked: {root}"
                )
            archived_root = _archive_existing_root(root)
        root.mkdir(parents=True, exist_ok=True)
        return {
            "output_root": root,
            "archived_root": None if archived_root is None else str(archived_root.resolve()),
            "existing_summary": None,
        }

    if reuse_mode == "resume":
        root.mkdir(parents=True, exist_ok=True)
        if existing_summary is not None:
            _clear_lock_best_effort(lock_path)
            return {
                "output_root": root,
                "archived_root": None,
                "existing_summary": existing_summary,
            }
        if lock_path.exists():
            lock_payload = _load_json(lock_path)
            raise CoordinationError(
                f"{entity_name} appears active and cannot be resumed while locked: {root}; "
                f"lock={lock_payload}"
            )
        if existing_summary is not None:
            return {
                "output_root": root,
                "archived_root": None,
                "existing_summary": existing_summary,
            }
        if _has_contents(root):
            raise CoordinationError(
                f"{entity_name} contains partial artifacts without {summary_filename}: {root}. "
                "Use restart or choose a fresh output root."
            )
        return {
            "output_root": root,
            "archived_root": None,
            "existing_summary": None,
        }

    raise CoordinationError(f"unsupported run reuse mode: {reuse_mode}")


@dataclass
class DirectoryLock:
    output_root: Path
    lock_filename: str
    payload: Dict[str, Any]
    owner_token: str

    @property
    def path(self) -> Path:
        return self.output_root / self.lock_filename

    def release(self) -> None:
        if not self.path.exists():
            return
        payload = _load_json(self.path)
        if payload and str(payload.get("owner_token") or "") != self.owner_token:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except PermissionError:
            try:
                released_path = self.path.with_name(f"{self.path.name}.released.{self.owner_token}")
                self.path.replace(released_path)
            except Exception:
                return

    def __enter__(self) -> "DirectoryLock":
        self.output_root.mkdir(parents=True, exist_ok=True)
        lock_payload = {
            **self.payload,
            "owner_token": self.owner_token,
            "pid": int(os.getpid()),
            "hostname": socket.gethostname(),
            "created_at_utc": utc_now(),
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(lock_payload, indent=2))
        except FileExistsError as exc:
            existing = _load_json(self.path)
            raise CoordinationError(
                f"lock already exists for {self.output_root}: {self.path}; lock={existing}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_directory_lock(
    output_root: Path,
    *,
    lock_filename: str,
    entity_name: str,
    manifest_hash: Optional[str],
) -> DirectoryLock:
    root = Path(output_root).resolve()
    return DirectoryLock(
        output_root=root,
        lock_filename=lock_filename,
        payload={
            "entity_name": entity_name,
            "output_root": str(root),
            "manifest_hash": str(manifest_hash or ""),
        },
        owner_token=str(uuid.uuid4()),
    )


__all__ = [
    "CoordinationError",
    "DirectoryLock",
    "RunReuseMode",
    "acquire_directory_lock",
    "prepare_output_root",
]
