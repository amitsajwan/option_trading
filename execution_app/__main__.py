"""execution_app — broker execution service.

Subscribes to trade signals and routes them to the configured broker adapter.

Selected adapter:
  EXECUTION_ADAPTER=paper   PaperAdapter  (default — safe, no real orders)
  EXECUTION_ADAPTER=kite    KiteAdapter   (requires KITE_API_KEY + KITE_ACCESS_TOKEN)
  EXECUTION_ADAPTER=dhan    DhanAdapter   (requires DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN)

Health endpoint:
  GET /health  → {"status": "ok", "adapter": "paper|kite|dhan"}
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import uvicorn
from fastapi import FastAPI

from contracts_app import configure_ist_logging

from .adapter.base import BrokerAdapter
from .adapter.dhan import DhanAdapter
from .adapter.kite import KiteAdapter
from .adapter.paper import PaperAdapter
from .adapter.shadow import ShadowAdapter
from .consumer import ExecutionConsumer
from .fill_tracker import FillTracker

configure_ist_logging()
logger = logging.getLogger(__name__)

_ADAPTER_ENV = str(os.getenv("EXECUTION_ADAPTER", "paper") or "paper").strip().lower()
_FILL_TRACKER_ENABLED = str(os.getenv("FILL_TRACKER_ENABLED", "1") or "1").strip() not in {"0", "false", "no"}
_HEALTH_PORT = int(os.getenv("EXECUTION_APP_HEALTH_PORT", "8009") or "8009")

app = FastAPI(title="execution_app", docs_url=None, redoc_url=None)

_adapter_name: str = _ADAPTER_ENV


@app.get("/health")
def health():
    return {"status": "ok", "adapter": _adapter_name}


def _build_adapter() -> BrokerAdapter:
    global _adapter_name
    if _ADAPTER_ENV == "kite":
        _adapter_name = "kite"
        return KiteAdapter()
    if _ADAPTER_ENV == "dhan":
        _adapter_name = "dhan"
        return DhanAdapter()
    if _ADAPTER_ENV == "shadow":
        _adapter_name = "shadow"
        return ShadowAdapter()
    _adapter_name = "paper"
    logger.info("execution_app: using PaperAdapter (EXECUTION_ADAPTER=%s)", _ADAPTER_ENV)
    return PaperAdapter()


def _run_consumer(adapter: BrokerAdapter) -> None:
    consumer = ExecutionConsumer(adapter)
    logger.info("execution_app: consumer thread starting")
    consumer.run()


def _run_fill_tracker() -> None:
    if not _FILL_TRACKER_ENABLED:
        logger.info("execution_app: fill tracker disabled (FILL_TRACKER_ENABLED=0)")
        return
    tracker = FillTracker()
    logger.info("execution_app: fill tracker thread starting")
    tracker.run()


def main() -> None:
    adapter = _build_adapter()
    logger.info("execution_app: starting adapter=%s health_port=%d", _adapter_name, _HEALTH_PORT)

    consumer_thread = threading.Thread(target=_run_consumer, args=(adapter,), daemon=True, name="execution-consumer")
    consumer_thread.start()

    fill_thread = threading.Thread(target=_run_fill_tracker, daemon=True, name="fill-tracker")
    fill_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=_HEALTH_PORT, log_config=None)


if __name__ == "__main__":
    main()
