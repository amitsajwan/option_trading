"""Query the ml_pipeline_runs results registry -- replaces "which memory
file / VM log had that result again?" with one command.

    python3 -m ml_pipeline_2.scripts.pipeline_report --instrument NIFTY
    python3 -m ml_pipeline_2.scripts.pipeline_report --study-id label_target_2026-09-09
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--study-id", default=None)
    parser.add_argument("--node", default=None, choices=["train", "backtest", "deploy_decision"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mongo-host", default="mongo")
    args = parser.parse_args(argv)

    sys.path.insert(0, "/app")
    from pymongo import MongoClient
    from ml_pipeline_2.pipeline.registry import RunRegistry

    registry = RunRegistry(MongoClient(args.mongo_host, 27017))
    docs = registry.history(instrument=args.instrument, study_id=args.study_id, node=args.node, limit=args.limit)

    if not docs:
        print("No runs found matching that filter.", file=sys.stderr)
        return 1

    for doc in docs:
        doc["_id"] = str(doc["_id"])
        doc["created_at"] = str(doc["created_at"])
    print(json.dumps(docs, indent=2, default=str))
    print(f"\n{len(docs)} run(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
