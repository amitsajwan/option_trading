"""Reproducibility manifest for sim/replay runs.

Every sim run writes a ``manifest.json`` into its run-dir and a matching
row into ``strategy_eval_runs``. The manifest captures everything required
to *reproduce the same outputs* from the same source date: the engine
code (git_commit), the container image (image_digest), the config
(env_overrides + config_hash), and the source corpus.

See also:
    docs/SCRUM_BOARD_SIM_REPLAY.md  (SIM-1)
    memory/project_sim_replay_design_2026-05-27  (design doc)
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from .sim_namespace import Kind

TerminalStatus = Literal["running", "completed", "failed", "cancelled"]

_VALID_STATUSES: tuple[TerminalStatus, ...] = (
    "running",
    "completed",
    "failed",
    "cancelled",
)


@dataclass(frozen=True)
class SimManifest:
    """Frozen description of one sim/replay run.

    Fields are write-once. To update a run's status, write a *new* event
    row to ``strategy_eval_runs`` rather than mutating an existing manifest.
    """

    run_id: str
    kind: Kind
    source_date: str
    source_coll: str
    label: str
    git_commit: str
    config_hash: str
    env_overrides: Mapping[str, str]
    image_digest: str
    speed: float
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    terminal_status: TerminalStatus = "running"
    sentinel_id: Optional[str] = None

    def __post_init__(self) -> None:  # pragma: no cover - trivial
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.source_date:
            raise ValueError("source_date is required")
        if not self.source_coll:
            raise ValueError("source_coll is required")
        if self.terminal_status not in _VALID_STATUSES:
            raise ValueError(
                f"unknown terminal_status={self.terminal_status!r}; "
                f"expected one of {_VALID_STATUSES}"
            )
        if not isinstance(self.env_overrides, Mapping):
            raise TypeError("env_overrides must be a Mapping[str, str]")

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        # Coerce Mapping → plain dict for clean JSON round-tripping.
        out: dict[str, Any] = asdict(self)
        out["env_overrides"] = dict(self.env_overrides)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "SimManifest":
        return cls.from_dict(json.loads(raw))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SimManifest":
        env = data.get("env_overrides") or {}
        if not isinstance(env, Mapping):
            raise TypeError("env_overrides in manifest dict must be a mapping")
        return cls(
            run_id=str(data["run_id"]),
            kind=str(data["kind"]),  # type: ignore[arg-type]
            source_date=str(data["source_date"]),
            source_coll=str(data["source_coll"]),
            label=str(data.get("label") or ""),
            git_commit=str(data.get("git_commit") or "unknown"),
            config_hash=str(data["config_hash"]),
            env_overrides=dict(env),
            image_digest=str(data.get("image_digest") or "unknown"),
            speed=float(data.get("speed") or 0.0),
            created_at=str(data["created_at"]),
            started_at=(str(data["started_at"]) if data.get("started_at") else None),
            completed_at=(str(data["completed_at"]) if data.get("completed_at") else None),
            terminal_status=str(data.get("terminal_status") or "running"),  # type: ignore[arg-type]
            sentinel_id=(str(data["sentinel_id"]) if data.get("sentinel_id") else None),
        )

    # ── filesystem write (write-once, atomic) ────────────────────────────
    def write_to(self, run_dir: Path) -> Path:
        """Write ``manifest.json`` atomically into ``run_dir``.

        Raises ``FileExistsError`` if a manifest is already present — by
        design, manifests are immutable; update events go to mongo.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "manifest.json"
        if target.exists():
            raise FileExistsError(
                f"manifest already exists at {target}; manifests are immutable"
            )
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(target)
        return target


def compute_config_hash(
    *,
    env_overrides: Mapping[str, str],
    image_digest: str,
    speed: float,
) -> str:
    """Deterministic SHA-256 of the inputs that affect a run's outputs.

    Two runs with identical (env_overrides, image_digest, speed) produce
    the same config_hash regardless of dict insertion order or whitespace.
    The same source_date + same config_hash + same git_commit MUST yield
    identical outputs — that's the reproducibility contract.
    """
    payload: dict[str, Any] = {
        "env_overrides": {k: str(v) for k, v in sorted(dict(env_overrides).items())},
        "image_digest": str(image_digest),
        "speed": float(speed),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolve_git_commit(*, repo_root: Optional[Path] = None) -> str:
    """Best-effort ``git rev-parse HEAD``; returns ``"unknown"`` on failure.

    Never raises — manifest writers should always be able to produce a
    manifest, even if git is unavailable. Callers that require a real SHA
    should validate the result themselves.
    """
    try:
        cwd = str(repo_root) if repo_root else None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"


__all__ = [
    "SimManifest",
    "TerminalStatus",
    "compute_config_hash",
    "resolve_git_commit",
]
