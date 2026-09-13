"""Single source of truth for each instrument's current verified entry
candidate, and a CLI to re-run any of them through the SAME registry-
backed path as ``pipeline_backtest.py`` (never a throwaway scratch
script) so every re-run is traceable in Mongo's ``ml_pipeline_runs``
collection.

Why this exists (2026-09-13): the 2026-09-11/12 five-instrument
investigation ran every backtest via ad-hoc scratch scripts in /tmp
that called ``run_backtest()`` directly. None of them were registry-
tracked. When SENSEX's "closed negative" verdict later turned out to
be unreproducible, there was no log, no Mongo record, and no surviving
script to check -- the exact parameters were simply gone. See
project_sensex_entry_system_status_2026-09-11.md's "root cause is
unrecoverable" section for the full story. This file exists so that
never happens again: every candidate's exact window/model/threshold/
config lives here, in git, not in someone's terminal history.

Usage
-----
    # Print a candidate's exact config (no run):
    python3 -m ml_pipeline_2.scripts.verified_candidates show SENSEX

    # Re-run a candidate through the registry-backed path:
    python3 -m ml_pipeline_2.scripts.verified_candidates run SENSEX \\
        --label "re-verify after Sept deploy"

    # List every known candidate:
    python3 -m ml_pipeline_2.scripts.verified_candidates list

Adding a new verified candidate: add an entry to CANDIDATES below with
every field filled in from the actual investigation that verified it --
do not add a candidate here without a real backtest result to back it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Candidate:
    instrument: str
    model_path: str
    threshold: float
    date_from: str
    date_to: str
    holdout_end: str
    extra_config: dict[str, str] = field(default_factory=dict)
    verified_result: str = ""  # human-readable summary of the result when last verified
    memory_ref: str = ""       # which memory file / date this came from
    notes: str = ""


CANDIDATES: dict[str, Candidate] = {
    "BANKNIFTY": Candidate(
        instrument="BANKNIFTY",
        model_path="/app/models/banknifty_entry_010pct_v1.joblib",
        threshold=0.30,
        date_from="2026-08-03",
        date_to="2026-09-03",
        holdout_end="2026-07-31",
        extra_config={
            "EXIT_GIVEBACK_STOP_ENABLED": "true",
            "EXIT_GIVEBACK_MIN_MFE": "0.008",
            "EXIT_GIVEBACK_PCT": "0.010",
            "EXIT_TRAILING_ACTIVATION_PCT": "0.008",
            "EXIT_TRAILING_TRAIL_PCT": "0.003",
            "EXIT_MAX_LOSS_PCT": "0.05",
            # Pinned 2026-09-13 -- these used to be inherited silently from
            # whatever the live container's ambient env happened to be, which
            # is exactly what let the EXIT_STRATEGY_MODE bug (see notes) go
            # undetected: a re-run's result depended on config that lived
            # nowhere in this file. Now deployed as BANKNIFTY_EXIT_STRATEGY_MODE
            # in docker-compose.gcp.yml too, so this is belt-and-suspenders,
            # not the only place it's set.
            "EXIT_STRATEGY_MODE": "scalper",
            "EXIT_PREMIUM_TARGET_PCT": "0.015",
            "EXIT_SCALPER_STALE_BARS": "15",
        },
        verified_result="17 live trades under EXIT_STRATEGY_MODE=scalper (re-verified "
                         "2026-09-13; the original '14 trades, PF=1.95' figure was run "
                         "under the ambient EXIT_STRATEGY_MODE=adaptive that was live at "
                         "the time -- NOT reproducible from this file alone until the pin "
                         "above was added. See memory_ref for the full story.)",
        memory_ref="project_banknifty_entry_system_status_2026-09-11 (stop-tuned "
                   "2026-09-12); project_straddle_design_proxy_test_2026-09-13 "
                   "(EXIT_STRATEGY_MODE bug found + fixed, harness hermeticity gap)",
        notes="Most heavily scrutinized candidate -- 5+ pipeline bugs found and "
              "fixed here first, direction forensics done (dead). Deployed paper-only. "
              "2026-09-13: found EXIT_STRATEGY_MODE was a single unnamespaced switch "
              "shared by all 5 instruments and defaulting to 'adaptive' live -- 9 of 17 "
              "real trades were routing through a never-tuned lottery sub-stack, losing "
              "-5.58% vs. a forced-scalper -1.53% on the same trades. Fixed at the infra "
              "level (namespaced + pinned to scalper) AND pinned here for hermeticity.",
    ),
    "NIFTY": Candidate(
        instrument="NIFTY",
        model_path="/app/models/nifty_entry_010pct_v1.joblib",
        threshold=0.50,
        date_from="2026-08-03",
        date_to="2026-09-03",
        holdout_end="2026-07-31",
        extra_config={
            "EXIT_MAX_LOSS_PCT": "0.15",
            # Pinned 2026-09-13, same reasoning as BankNifty above. NIFTY's
            # 5-trade sample never actually exercised the lottery path (0/5
            # were BREAKOUT/TRENDING at entry) so this is a no-known-effect
            # hermeticity fix, not a result-changing one.
            "EXIT_STRATEGY_MODE": "scalper",
        },
        verified_result="5 live trades, 80% win, PF=4.71, +4.26% return",
        memory_ref="project_nifty_entry_system_status_2026-09-11 (stop-tuned 2026-09-12)",
        notes="Thinnest sample of the deployed candidates (5 trades) -- do not "
              "over-weight the flashy PF. Exit-policy tuning is OPPOSITE direction "
              "from BankNifty (wider stop, not tighter). Deployed paper-only.",
    ),
    "SENSEX": Candidate(
        instrument="SENSEX",
        model_path="/app/models/sensex_entry_010pct_v1.joblib",
        threshold=0.50,
        date_from="2026-07-16",
        date_to="2026-08-06",
        holdout_end="2026-07-15",
        extra_config={
            # Pinned 2026-09-13 to the live container's actual ambient values
            # at verification time (confirmed via `docker exec ... env`) --
            # NOT changed from what was actually running, just made explicit
            # so a future re-run can't silently drift if the live default
            # ever changes. SENSEX has NOT been tested the way BankNifty/NIFTY
            # were for whether adaptive/lottery routing helps or hurts here --
            # this freezes current (untested) behavior, it doesn't validate it.
            "EXIT_STRATEGY_MODE": "adaptive",
            "EXIT_GIVEBACK_STOP_ENABLED": "true",
            "EXIT_GIVEBACK_MIN_MFE": "0.03",
            "EXIT_GIVEBACK_PCT": "0.09",
        },
        verified_result="8 live trades, 75% win, PF=3.06, +4.02% return",
        memory_ref="project_sensex_entry_system_status_2026-09-11 (REOPENED 2026-09-12/13)",
        notes="Window is SENSEX's own holdout period -- its history ends 2026-08-06 "
              "with no post-holdout room, unlike the other instruments. REOPENED "
              "after the original 'closed negative' verdict could not be reproduced "
              "and its root cause was never found (6 hypotheses ruled out) -- see "
              "memory_ref for the full investigation. Deployed paper-only, "
              "operator_halt added specifically for this deploy. NOT yet tested for "
              "the EXIT_STRATEGY_MODE=adaptive/lottery-routing issue found on "
              "BankNifty -- see extra_config comment.",
    ),
    "MIDCPNIFTY": Candidate(
        instrument="MIDCPNIFTY",
        model_path="/app/models/midcpnifty_entry_010pct_v1.joblib",
        threshold=0.60,
        date_from="2026-08-08",
        date_to="2026-09-03",
        holdout_end="2026-08-06",
        extra_config={
            "EXIT_MAX_LOSS_PCT": "0.01",
            # Pinned 2026-09-13, same reasoning as SENSEX above -- freezes
            # current (untested) ambient behavior, doesn't validate it.
            "EXIT_STRATEGY_MODE": "adaptive",
            "EXIT_GIVEBACK_STOP_ENABLED": "true",
            "EXIT_GIVEBACK_MIN_MFE": "0.03",
            "EXIT_GIVEBACK_PCT": "0.09",
        },
        verified_result="13 live trades, 53.8% win, PF=4.84, +1.64% return",
        memory_ref="project_midcpnifty_entry_system_status_2026-09-11 (stop-tuned 2026-09-12)",
        notes="date_from is 2026-08-08, NOT 08-07 (the naive post-holdout guess was "
              "off by one day -- verified by exact reproduction before trusting any "
              "sweep). Strongest PF of any instrument, but least scrutinized (no "
              "direction forensics, deliberately deprioritized). First-ever paper "
              "deploy of this instrument -- operator_halt added proactively since "
              "'--rollout-stage paper' does NOT actually gate execution in code.",
    ),
}


def _run(candidate: Candidate, label: Optional[str], no_registry: bool) -> int:
    sys.path.insert(0, "/app")
    from ml_pipeline_2.pipeline.backtest_runner import build_backtest_result, run_backtest
    from ml_pipeline_2.pipeline.validation import (
        BacktestWindowContaminated,
        ModelDegenerate,
        ParquetInstrumentMismatch,
    )
    import joblib

    bundle = joblib.load(candidate.model_path)
    holdout_eval = bundle.get("holdout_eval")

    try:
        summary = run_backtest(
            instrument=candidate.instrument,
            model_path=candidate.model_path,
            entry_min_prob=candidate.threshold,
            date_from=candidate.date_from,
            date_to=candidate.date_to,
            holdout_end=candidate.holdout_end,
            label=label or f"verified_candidates re-run: {candidate.instrument}",
            holdout_eval=holdout_eval,
            extra_config=dict(candidate.extra_config),
        )
    except (BacktestWindowContaminated, ModelDegenerate, ParquetInstrumentMismatch) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    result = build_backtest_result(summary)
    print(f"\n=== {summary.label} ===")
    print(json.dumps(result, indent=2, default=str))
    print(f"\nExpected (last verified): {candidate.verified_result}", file=sys.stderr)
    if summary.is_thin_sample:
        print(f"\n⚠️  THIN SAMPLE: only {summary.live_trades} live trades.", file=sys.stderr)

    if not no_registry:
        from pymongo import MongoClient
        from ml_pipeline_2.pipeline.registry import RunRegistry, build_run_document

        verdict = "inconclusive" if summary.is_thin_sample else ("usable" if summary.live_trades > 0 else "degenerate")
        doc = build_run_document(
            instrument=candidate.instrument, study_id="verified_candidates_reverify",
            node="backtest",
            config={
                "candidate_id": f"{candidate.instrument}_verified", "model_path": candidate.model_path,
                "threshold": candidate.threshold, "date_from": candidate.date_from,
                "date_to": candidate.date_to, "holdout_end": candidate.holdout_end,
                "extra_config": candidate.extra_config,
            },
            result=result, verdict=verdict, artifact_path=candidate.model_path,
        )
        registry = RunRegistry(MongoClient("mongo", 27017))
        run_id = registry.record(doc)
        print(f"\nRecorded to ml_pipeline_runs: {run_id}", file=sys.stderr)

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="list every known verified candidate")

    p_show = sub.add_parser("show", help="print one candidate's exact config")
    p_show.add_argument("instrument")

    p_run = sub.add_parser("run", help="re-run a candidate through the registry-backed path")
    p_run.add_argument("instrument")
    p_run.add_argument("--label", default=None)
    p_run.add_argument("--no-registry", action="store_true")

    args = parser.parse_args(argv)

    if args.action == "list":
        for name, c in CANDIDATES.items():
            print(f"{name}: thr={c.threshold} window={c.date_from}..{c.date_to} -- {c.verified_result}")
        return 0

    instrument = args.instrument.upper()
    if instrument not in CANDIDATES:
        print(f"Unknown instrument {instrument!r}. Known: {list(CANDIDATES)}", file=sys.stderr)
        return 1
    candidate = CANDIDATES[instrument]

    if args.action == "show":
        print(json.dumps(candidate.__dict__, indent=2))
        return 0

    if args.action == "run":
        return _run(candidate, args.label, args.no_registry)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
