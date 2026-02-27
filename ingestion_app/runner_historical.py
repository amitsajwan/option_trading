#!/usr/bin/env python3
"""Standalone historical replay runner for use by ingestion_app.runner.

This module runs historical replay in a separate process, similar to the old start_historical.py.
It's called by ingestion_app.runner when --mode historical is used.
"""
import argparse
import asyncio

from .runtime import monitor_for_ticks, resolve_historical_replay_config, run_historical_replay


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical-source", type=str, help="zerodha, synthetic, or path/to/file.csv")
    parser.add_argument("--historical-speed", type=float, help="Playback speed multiplier")
    parser.add_argument("--historical-from", type=str, help="YYYY-MM-DD")
    parser.add_argument("--historical-ticks", action="store_true", help="Use tick-level replay")
    args = parser.parse_args()

    config = resolve_historical_replay_config(
        historical_source=args.historical_source,
        historical_speed=args.historical_speed,
        historical_from=args.historical_from,
        historical_ticks=args.historical_ticks,
    )
    asyncio.run(run_historical_replay(config))


if __name__ == "__main__":
    main()
