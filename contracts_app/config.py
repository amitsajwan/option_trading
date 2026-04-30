from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency at runtime
    load_dotenv = None


def _detect_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "ingestion_app").exists() and (parent / "snapshot_app").exists():
            return parent
    return current.parents[1]


def _load_dotenv_candidates() -> None:
    """Load env files in a stable order without overriding existing shell vars."""
    if load_dotenv is None:
        return
    repo_root = _detect_repo_root()
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        repo_root / "ingestion_app" / ".env",
        repo_root / "snapshot_app" / ".env",
    ]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            continue
        try:
            load_dotenv(dotenv_path=path, override=False)
        except Exception:
            continue


_load_dotenv_candidates()


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def redis_connection_kwargs(
    *,
    decode_responses: bool = True,
    for_pubsub: bool = False,
) -> Dict[str, Any]:
    """Canonical Redis connection settings for top-level process apps."""
    host = str(os.getenv("REDIS_HOST") or os.getenv("DEFAULT_REDIS_HOST") or "localhost")
    port = _env_int("REDIS_PORT", _env_int("DEFAULT_REDIS_PORT", 6379))
    db = _env_int("REDIS_DB", _env_int("DEFAULT_REDIS_DB", 0))
    connect_timeout = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT") or "2")
    socket_timeout: Optional[float]
    if for_pubsub:
        # PubSub listeners are long-lived blocking reads; timeout=None prevents false failures.
        socket_timeout = None
    else:
        socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT") or "2")
    return {
        "host": host,
        "port": port,
        "db": db,
        "decode_responses": bool(decode_responses),
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
    }
