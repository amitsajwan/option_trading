"""Strategy app entrypoint (Layer 4 consumer)."""

from __future__ import annotations

import argparse
import logging
from typing import Iterable, Optional

from contracts_app import snapshot_topic

from .engines import DeterministicRuleEngine, MLRegimeEngine
from .runtime import RedisSnapshotConsumer

logger = logging.getLogger(__name__)


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy app redis consumer runtime.")
    parser.add_argument("--engine", choices=["deterministic", "ml"], default="deterministic")
    parser.add_argument("--topic", default=None, help=f"Snapshot topic (default: {snapshot_topic()})")
    parser.add_argument("--poll-interval-sec", type=float, default=0.2)
    parser.add_argument("--max-events", type=int, default=0, help="Stop after N events (0 = infinite)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.engine == "deterministic":
        engine = DeterministicRuleEngine()
    else:
        engine = MLRegimeEngine(delegate=DeterministicRuleEngine())

    topic = str(args.topic or snapshot_topic()).strip() or snapshot_topic()
    consumer = RedisSnapshotConsumer(
        engine=engine,
        topic=topic,
        poll_interval_sec=max(0.01, float(args.poll_interval_sec)),
    )
    max_events = None if int(args.max_events) <= 0 else int(args.max_events)
    logger.info("strategy_app starting engine=%s topic=%s", args.engine, topic)
    consumed = consumer.start(max_events=max_events)
    logger.info("strategy_app consumed events=%s", consumed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    raise SystemExit(run_cli())
