"""Shared environment resolution helpers for ingestion services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _detect_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "ingestion_app").exists() and (parent / "snapshot_app").exists():
            return parent
    return current.parents[1]


def _load_dotenv_candidates() -> None:
    if load_dotenv is None:
        return

    repo_root = _detect_repo_root()
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        repo_root / "ingestion_app" / ".env",
        repo_root / "market_data_dashboard" / ".env",
    ]
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            try:
                load_dotenv(dotenv_path=path, override=False)
            except Exception:
                pass


_load_dotenv_candidates()


def env_str(name: str, default: Optional[str] = None) -> str:
    raw = (os.getenv(name) or "").strip()
    if raw:
        return raw
    return "" if default is None else str(default)


def env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def resolve_instrument_symbol(default: Optional[str] = None) -> str:
    symbol = env_str("INSTRUMENT_SYMBOL")
    if symbol:
        return symbol
    key = env_str("INSTRUMENT_KEY")
    if key:
        return key
    trading_symbol = env_str("INSTRUMENT_TRADING_SYMBOL")
    if trading_symbol:
        return trading_symbol
    fallback = env_str("DEFAULT_INSTRUMENT_SYMBOL")
    if fallback:
        return fallback
    if default is not None:
        return str(default)
    return "INSTRUMENT_NOT_SET"


def redis_config(*, decode_responses: bool = True, db: Optional[int] = None) -> dict:
    redis_db = env_int("REDIS_DB", 0) if db is None else int(db)
    return {
        "host": env_str("REDIS_HOST", "localhost"),
        "port": env_int("REDIS_PORT", 6379),
        "db": redis_db,
        "decode_responses": bool(decode_responses),
    }


def mongo_config() -> dict:
    uri = env_str("MONGODB_URI") or env_str("MONGO_URI")
    if uri:
        return {"uri": uri}
    return {
        "host": env_str("MONGO_HOST", "localhost"),
        "port": env_int("MONGO_PORT", 27017),
        "db": env_str("MONGO_DB", "trading_ai"),
    }


def credentials_path_candidates(filename: str = "credentials.json") -> list[Path]:
    repo_root = _detect_repo_root()
    explicit = env_str("KITE_CREDENTIALS_PATH")
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    paths.extend(
        [
            Path.cwd() / filename,
            repo_root / filename,
        ]
    )

    seen = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
