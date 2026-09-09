"""The unattended overnight entry point: "train all instruments, go to
sleep, come back and see what happened, why, and which model won."

**Always dry-run a config before trusting it for real** (proves the whole
pipeline executes end-to-end in minutes -- data load, HPO, calibration,
holdout eval, ship-gate check, backtest window validation, backtest,
registry recording, selection -- without waiting hours; the results
themselves are statistically meaningless, this only proves nothing
crashes):

    python3 -m ml_pipeline_2.scripts.pipeline_run_all \\
        --config ml_pipeline_2/configs/overnight_study_2026-09-10.json \\
        --study-id overnight_2026-09-10 --dry-run

Run detached on the VM so it survives an SSH disconnect (this is a
multi-hour job -- do not run it in a foreground shell you might close):

    cd /opt/option_trading
    sudo docker run --rm -d --name overnight_study \\
        -v "$(pwd)/.data:/app/.data" -v "$(pwd)/models:/app/models" \\
        --network option_trading_default \\
        option_trading-strategy_app:latest \\
        python3 -m ml_pipeline_2.scripts.pipeline_run_all \\
            --config ml_pipeline_2/configs/overnight_study_2026-09-10.json \\
            --study-id overnight_2026-09-10

    # check on it later:
    sudo docker logs -f overnight_study
    # or, once it's finished (container exits after the run completes):
    sudo docker logs overnight_study > overnight_study.log 2>&1

The config JSON is a list of per-instrument study plans (see
ml_pipeline_2.pipeline.study_runner.InstrumentStudyPlan /
CandidatePlan for the exact fields) -- deliberately explicit about every
candidate's train/valid/holdout/backtest windows rather than
auto-derived, since getting that window right (clean of the training
window) was this session's biggest incident
(project_backtest_insample_contamination_finding_2026-09-09.md).

This script trains, backtests, records, and selects. It NEVER deploys
anything or touches a live trading container -- see the option-trading
skill's Deployment process for that separate, explicit, human-approved
step. It also never runs anything in parallel: this session found
XGBoost's own internal parallelism means running multiple training
processes at once on this VM's 4 cores is oversubscription, not
speedup -- sequential is both simpler and measurably faster wall-clock.

To verify what happened after waking up:
    python3 -m ml_pipeline_2.scripts.pipeline_select --instrument NIFTY --study-id overnight_2026-09-10
    python3 -m ml_pipeline_2.scripts.pipeline_report --study-id overnight_2026-09-10
or read the summary this script itself prints/writes at the end.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_plans(config_path: str, study_id: str):
    from ml_pipeline_2.pipeline.study_runner import CandidatePlan, InstrumentStudyPlan

    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    plans = []
    for entry in payload:
        candidates = [CandidatePlan(**c) for c in entry["candidates"]]
        plans.append(InstrumentStudyPlan(
            instrument=entry["instrument"],
            study_id=study_id,
            data_dir=entry["data_dir"],
            output_dir=entry["output_dir"],
            candidates=candidates,
        ))
    return plans


def _real_train_fn(*, instrument: str, data_dir: str, output: str,
                    train_start: str, train_end: str, valid_start: str, valid_end: str,
                    holdout_start: str, holdout_end: str, n_trials: int, label_pct: float) -> dict:
    import joblib
    from ml_pipeline_2.scripts.train_entry_dhan_v3 import main as train_main

    argv = [
        "--instrument", instrument, "--data-dir", data_dir, "--output", output,
        "--train-start", train_start, "--train-end", train_end,
        "--valid-start", valid_start, "--valid-end", valid_end,
        "--holdout-start", holdout_start, "--holdout-end", holdout_end,
        "--n-trials", str(n_trials), "--label-pct", str(label_pct),
    ]
    exit_code = train_main(argv)
    if exit_code not in (0, 2):  # 2 = ran OK but ship gates failed -- still a usable bundle to inspect
        raise RuntimeError(f"train_entry_dhan_v3 exited {exit_code} for {instrument} label_pct={label_pct}")
    bundle = joblib.load(output)
    return {
        "holdout_eval": bundle.get("holdout_eval"),
        "ship_gates": bundle.get("ship_gates"),
        "ship_gates_all_pass": bundle.get("ship_gates_all_pass"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="path to the study plan JSON (see module docstring)")
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--mongo-host", default="mongo")
    parser.add_argument("--summary-out", default=None,
                         help="also write the final summary to this file path")
    parser.add_argument("--dry-run", action="store_true",
                         help="clip every candidate's date windows to --dry-run-days each "
                              "(chained from its own train_start) and cut HPO trials to "
                              "--dry-run-trials -- proves the whole pipeline runs end-to-end "
                              "in minutes, statistically meaningless by design. Run this before "
                              "trusting a config for a real multi-hour unattended run.")
    parser.add_argument("--dry-run-days", type=int, default=5)
    parser.add_argument("--dry-run-trials", type=int, default=3)
    args = parser.parse_args(argv)

    sys.path.insert(0, "/app")
    from pymongo import MongoClient
    from ml_pipeline_2.pipeline.backtest_runner import run_backtest
    from ml_pipeline_2.pipeline.registry import RunRegistry
    from ml_pipeline_2.pipeline.study_runner import format_summary, run_all_instruments, shrink_plan_for_dry_run

    study_id = f"{args.study_id}_dryrun" if args.dry_run else args.study_id
    plans = _load_plans(args.config, study_id)
    if args.dry_run:
        plans = [shrink_plan_for_dry_run(p, max_days=args.dry_run_days, n_trials=args.dry_run_trials) for p in plans]
        print(f"DRY RUN: windows clipped to {args.dry_run_days} days, HPO trials cut to {args.dry_run_trials}. "
              f"Results are NOT statistically meaningful -- this only proves the pipeline runs. "
              f"study_id suffixed '_dryrun' so it can't be confused with a real study.", file=sys.stderr)
    registry = RunRegistry(MongoClient(args.mongo_host, 27017))

    def backtest_fn(**kwargs) -> dict:
        from ml_pipeline_2.pipeline.backtest_runner import build_backtest_result
        summary = run_backtest(**kwargs)
        return build_backtest_result(summary)

    print(f"Starting unattended study run: study_id={args.study_id}, {len(plans)} instrument(s), "
          f"sequential (never parallel).", file=sys.stderr)
    outcomes = run_all_instruments(plans, train_fn=_real_train_fn, backtest_fn=backtest_fn, registry=registry)

    summary = format_summary(outcomes)
    print(summary)
    if args.summary_out:
        Path(args.summary_out).write_text(summary, encoding="utf-8")
        print(f"\nSummary written to {args.summary_out}", file=sys.stderr)

    any_fatal = any(o.fatal_error for o in outcomes)
    any_decided = any(o.selection and o.selection.decision == "decided" for o in outcomes)
    if any_fatal:
        print("\nOne or more instruments hit a FATAL error -- see summary above.", file=sys.stderr)
    return 1 if (any_fatal and not any_decided) else 0


if __name__ == "__main__":
    raise SystemExit(main())
