"""Durable entrypoint for the seller daemon — `python -m strategy_app.seller`.

PAPER by default (no real orders) — runs the T9 live paper cycle. Real money requires
EXECUTION_ADAPTER=dhan AND SELLER_LIVE_ENABLED=1 (both must be set deliberately).

Config (env): SELLER_CONDOR_OFFSET, SELLER_IV_RANK_MIN, SELLER_SPREAD_WIDTH,
SELLER_TP_FRAC, SELLER_STOP_MULT, SELLER_MAX_HOLD_DAYS, SELLER_ENTRY_WINDOW,
SELLER_MAX_CONCURRENT, SELLER_DAILY_LOSS_CAP_RS, SELLER_LOOP_INTERVAL_S.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("seller.main")


def _build_gateway_factory():
    """Return a gateway factory: PAPER unless real money is explicitly enabled."""
    adapter = (os.getenv("EXECUTION_ADAPTER", "paper") or "paper").strip().lower()
    live_enabled = (os.getenv("SELLER_LIVE_ENABLED", "0") or "0").strip() in ("1", "true", "yes")
    if adapter == "dhan" and live_enabled:
        from .gateway import DhanLegGateway
        from execution_app.adapter.dhan import DhanAdapter
        dhan = DhanAdapter()
        log.warning("SELLER RUNNING ON REAL MONEY (Dhan) — EXECUTION_ADAPTER=dhan + SELLER_LIVE_ENABLED=1")
        return lambda pf: DhanLegGateway(dhan)
    log.info("seller gateway = PAPER (no real orders). adapter=%s live_enabled=%s", adapter, live_enabled)
    return None  # None -> SellerRunner uses PaperLegGateway


def main() -> None:
    run_dir = os.getenv("STRATEGY_RUN_DIR", "/tmp/seller_live")
    os.makedirs(run_dir, exist_ok=True)
    # sensible defaults (the validated config)
    os.environ.setdefault("SELLER_CONDOR_OFFSET", "200")
    os.environ.setdefault("SELLER_IV_RANK_MIN", "30")
    os.environ.setdefault("SELLER_SPREAD_WIDTH", "300")
    os.environ.setdefault("SELLER_TP_FRAC", "0.50")
    os.environ.setdefault("SELLER_STOP_MULT", "2.0")
    os.environ.setdefault("SELLER_MAX_HOLD_DAYS", "5")
    os.environ.setdefault("SELLER_ENTRY_WINDOW", "10:00-14:00")

    from pymongo import MongoClient
    from .runner import SellerRunner

    mongo_host = os.getenv("MONGO_HOST", "mongo")
    mongo_port = int(os.getenv("MONGO_PORT", "27017") or 27017)
    db = MongoClient(mongo_host, mongo_port)[os.getenv("MONGO_DB", "trading_ai")]
    runner = SellerRunner(db, gateway_factory=_build_gateway_factory())
    interval = float(os.getenv("SELLER_LOOP_INTERVAL_S", "30") or 30)
    runner.run_forever(interval_s=interval)


if __name__ == "__main__":
    main()
