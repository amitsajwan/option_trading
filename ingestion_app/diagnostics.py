"""Diagnostics and troubleshooting helpers for ingestion_app."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import redis

from .env_settings import credentials_path_candidates, redis_config


def gather_diagnostics() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "component": "ingestion_app",
        "env": {
            "REDIS_HOST": os.getenv("REDIS_HOST"),
            "REDIS_PORT": os.getenv("REDIS_PORT"),
            "REDIS_DB": os.getenv("REDIS_DB"),
            "INSTRUMENT_SYMBOL": os.getenv("INSTRUMENT_SYMBOL"),
            "KITE_CREDENTIALS_PATH": os.getenv("KITE_CREDENTIALS_PATH"),
            "MARKET_SESSION_ENABLED": os.getenv("MARKET_SESSION_ENABLED"),
        },
        "credentials_candidates": [str(p) for p in credentials_path_candidates()],
        "checks": [],
    }

    checks: List[Dict[str, Any]] = out["checks"]

    for path in credentials_path_candidates():
        checks.append(
            {
                "name": f"credentials_exists:{path}",
                "ok": bool(path.exists()),
                "detail": None if path.exists() else "missing",
            }
        )

    try:
        client = redis.Redis(**redis_config(decode_responses=True))
        client.ping()
        checks.append({"name": "redis_ping", "ok": True, "detail": "ok"})
    except Exception as exc:
        checks.append({"name": "redis_ping", "ok": False, "detail": str(exc)})

    return out


def print_diagnostics() -> None:
    payload = gather_diagnostics()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    print_diagnostics()
