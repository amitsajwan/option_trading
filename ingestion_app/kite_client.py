"""Shared KiteConnect client/session helpers.

Note: KiteConnect's `pool` argument expects a mapping of HTTPAdapter kwargs,
not a `requests.Session` object.
"""

from __future__ import annotations

import os
from typing import Optional

from urllib3.util.retry import Retry

try:
    from kiteconnect import KiteConnect
except Exception:  # pragma: no cover - optional dependency in some test contexts
    KiteConnect = None  # type: ignore

def _kite_pool_kwargs() -> dict:
    retries_total = int(os.getenv("KITE_HTTP_RETRIES", "5"))
    backoff_factor = float(os.getenv("KITE_HTTP_BACKOFF", "0.8"))
    backoff_jitter = float(os.getenv("KITE_HTTP_BACKOFF_JITTER", "0.6"))
    pool_connections = int(os.getenv("KITE_HTTP_POOL_CONNECTIONS", "20"))
    pool_maxsize = int(os.getenv("KITE_HTTP_POOL_MAXSIZE", "20"))
    pool_block = os.getenv("KITE_HTTP_POOL_BLOCK", "0").strip().lower() in ("1", "true", "yes")

    retry = Retry(
        total=retries_total,
        connect=retries_total,
        read=retries_total,
        backoff_factor=backoff_factor,
        backoff_jitter=backoff_jitter,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"HEAD", "GET", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    return {
        "pool_connections": pool_connections,
        "pool_maxsize": pool_maxsize,
        "max_retries": retry,
        "pool_block": pool_block,
    }


def create_kite_client(api_key: str, access_token: Optional[str] = None):
    """Create KiteConnect client bound to the shared HTTP session."""
    if KiteConnect is None:
        raise RuntimeError("kiteconnect is not available")
    timeout = int(os.getenv("KITE_HTTP_TIMEOUT", os.getenv("KITE_HTTP_READ_TIMEOUT", "30")))
    client = KiteConnect(api_key=api_key, pool=_kite_pool_kwargs(), timeout=timeout)
    if access_token:
        client.set_access_token(access_token)
    return client
