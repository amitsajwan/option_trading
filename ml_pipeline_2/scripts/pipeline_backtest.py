"""CLI entry point for the shared, instrument-agnostic capital-weighted
backtest -- replaces bespoke per-instrument scripts
(bn_labelgrid_bt.py, nifty_labelgrid_bt.py, sensex_v2_bt_rs.py, ...).

Run inside the strategy_app container (needs live Mongo/parquet access),
same as every backtest this session:

    python3 -m ml_pipeline_2.scripts.pipeline_backtest \\
        --instrument NIFTY \\
        --study-id label_target_2026-09-09 \\
        --model-path /app/.run/nifty_label_grid/nifty_015pct_v3_010pct.joblib \\
        --threshold 0.5 \\
        --from 2026-08-03 --to 2026-09-03 \\
        --holdout-end 2026-07-31 \\
        --label "0.10% target, moderate-freq regime"

Adding a new instrument needs a new InstrumentMLConfig entry in
ml_pipeline_2/pipeline/instrument_config.py -- NOT a new script.

Records the result to the ml_pipeline_runs Mongo collection via
--mongo-host (default: "mongo", the in-network service name; use
"localhost" when running as a bare process on the VM host instead of
inside a container) unless --no-registry is passed.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--holdout-end", required=True,
                         help="the model's own holdout_end date -- the backtest window is REJECTED "
                              "if it doesn't start strictly after this (in-sample contamination guard)")
    parser.add_argument("--min-gap-days", type=int, default=0)
    parser.add_argument("--label", default=None)
    parser.add_argument("--candidate-id", default=None,
                         help="friendly id for pipeline_select.py's ranking output; "
                              "defaults to --model-path if omitted")
    parser.add_argument("--mongo-host", default="mongo")
    parser.add_argument("--no-registry", action="store_true", help="skip recording to the results registry")
    parser.add_argument("--skip-degeneracy-check", action="store_true",
                         help="skip validating --threshold against the model's own separation_table "
                              "(only for models whose bundle doesn't carry holdout_eval)")
    args = parser.parse_args(argv)

    sys.path.insert(0, "/app")
    from ml_pipeline_2.pipeline.backtest_runner import build_backtest_result, run_backtest
    from ml_pipeline_2.pipeline.validation import (
        BacktestWindowContaminated,
        ModelDegenerate,
        ParquetInstrumentMismatch,
    )

    holdout_eval = None
    if not args.skip_degeneracy_check:
        import joblib
        bundle = joblib.load(args.model_path)
        holdout_eval = bundle.get("holdout_eval")

    try:
        summary = run_backtest(
            instrument=args.instrument,
            model_path=args.model_path,
            entry_min_prob=args.threshold,
            date_from=args.date_from,
            date_to=args.date_to,
            holdout_end=args.holdout_end,
            label=args.label,
            min_gap_days=args.min_gap_days,
            holdout_eval=holdout_eval,
        )
    except (BacktestWindowContaminated, ModelDegenerate, ParquetInstrumentMismatch) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    result = build_backtest_result(summary)
    print(f"\n=== {summary.label} ===")
    print(json.dumps(result, indent=2, default=str))
    if summary.is_thin_sample:
        print(f"\n⚠️  THIN SAMPLE: only {summary.live_trades} live trades -- "
              f"not enough to distinguish a real result from noise.", file=sys.stderr)

    if not args.no_registry:
        from pymongo import MongoClient
        from ml_pipeline_2.pipeline.registry import RunRegistry, build_run_document

        verdict = "inconclusive" if summary.is_thin_sample else ("usable" if summary.live_trades > 0 else "degenerate")
        doc = build_run_document(
            instrument=args.instrument, study_id=args.study_id, node="backtest",
            config={
                "candidate_id": args.candidate_id or args.model_path,
                "model_path": args.model_path, "threshold": args.threshold,
                "date_from": args.date_from, "date_to": args.date_to,
                "holdout_end": args.holdout_end,
            },
            result=result, verdict=verdict, artifact_path=args.model_path,
        )
        registry = RunRegistry(MongoClient(args.mongo_host, 27017))
        run_id = registry.record(doc)
        print(f"\nRecorded to ml_pipeline_runs: {run_id}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
