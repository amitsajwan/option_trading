"""The "Selection & Decision" node made concrete: query every backtest
result recorded for a study, apply this session's established decision
rules (hard-exclude contaminated/degenerate, never let a thin sample beat
a robust one), and print + record a winner -- instead of a human reading
result JSON out of memory files and eyeballing which config looks best
(the exact process that almost let NIFTY's contaminated 0.25% config,
which posted the best raw return of its whole study, pass as the winner).

    python3 -m ml_pipeline_2.scripts.pipeline_select \\
        --instrument NIFTY --study-id label_target_2026-09-09

Records the decision back to ml_pipeline_runs as a `node=deploy_decision`
document unless --no-registry is passed.
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--metric", default="return_on_capital_pct")
    parser.add_argument("--min-live-trades", type=int, default=15)
    parser.add_argument("--mongo-host", default="mongo")
    parser.add_argument("--no-registry", action="store_true", help="skip recording the decision to the results registry")
    args = parser.parse_args(argv)

    sys.path.insert(0, "/app")
    from pymongo import MongoClient
    from ml_pipeline_2.pipeline.candidates import select_best_candidate
    from ml_pipeline_2.pipeline.registry import RunRegistry, build_run_document

    registry = RunRegistry(MongoClient(args.mongo_host, 27017))
    records = registry.history(instrument=args.instrument, study_id=args.study_id, node="backtest", limit=200)

    if not records:
        print(f"No backtest runs found for instrument={args.instrument} study_id={args.study_id}.", file=sys.stderr)
        return 1

    selection = select_best_candidate(records, metric=args.metric, min_live_trades=args.min_live_trades)

    print(f"\n=== Selection: {args.instrument} / {args.study_id} ===")
    print(f"Decision: {selection.decision}")
    print(f"Winner: {selection.winner_candidate_id}")
    print(f"Reasoning: {selection.reasoning}\n")
    for rank in selection.ranked:
        marker = "EXCLUDED" if rank.excluded else "  "
        print(f"  [{marker}] {rank.candidate_id:30s} {args.metric}={rank.metric_value} "
              f"verdict={rank.verdict} thin={rank.is_thin_sample}"
              + (f"  ({rank.exclusion_reason})" if rank.exclusion_reason else ""))

    if not args.no_registry:
        doc = build_run_document(
            instrument=args.instrument, study_id=args.study_id, node="deploy_decision",
            config={"metric": args.metric, "min_live_trades": args.min_live_trades},
            result={
                "decision": selection.decision,
                "winner_candidate_id": selection.winner_candidate_id,
                "ranked": [rank.__dict__ for rank in selection.ranked],
            },
            verdict=selection.decision if selection.decision == "decided" else "inconclusive",
            notes=selection.reasoning,
        )
        run_id = registry.record(doc)
        print(f"\nRecorded deploy_decision: {run_id}", file=sys.stderr)

    return 0 if selection.decision == "decided" else 2


if __name__ == "__main__":
    raise SystemExit(main())
