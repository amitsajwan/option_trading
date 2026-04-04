# Current Evaluation Baseline (2026-04-04)

As-of date: `2026-04-04`

## Purpose

This note is the current run-scoped evidence checkpoint for the deterministic default stack.

Use it as the current source of truth ahead of older research notes when the question is:

- what is currently verified
- which run proved the replay stack is clean
- what still remains before deterministic replay research can be called closed

This document does not replace wider-window research. It records what is already verified on current code.

## Current Verified Product State

The following are now verified on current code:

- historical replay persistence is rerun-safe
- summary, trades, and session are all `run_id` scoped
- deterministic diagnostics are `run_id` scoped
- replay UI, operator docs, and evaluation compare UI are now aligned
- default deterministic profile is `det_core_v2`
- verified replay/session surfaces no longer mix old `det_core_v1` rows into current `det_core_v2` review

## Clean Verification Run

Primary clean verification run:

- `run_id`: `06dfa346-a7ba-4dce-b4f7-5c3eb43900c1`
- dataset: `historical`
- range: `2024-01-02` to `2024-01-05`

Clean verification summary:

- `votes`: `583`
- `signals`: `4`
- `closed_trades`: `2`
- `entry_strategy`: `ORB` only
- `exit_reason`: `TRAILING_STOP` only
- `exit_mechanism`: `ORB_TRAIL` only
- `win_rate`: `1.0`
- `gross_profit_capital_pct`: `1.6065%`
- `net_return_pct`: `2.2476465%`

Trade-level outcomes:

- `2024-01-04`: `ORB` `CE 48300`, `+867.0`, `+23.07%`, `ORB_TRAIL`
- `2024-01-05`: `ORB` `PE 48400`, `+739.5`, `+11.86%`, `ORB_TRAIL`

## What This Proves

This run is enough to prove:

- the current replay/evaluation plumbing is trustworthy for run-scoped analysis
- `det_core_v2` emits and persists deterministic votes correctly
- owner-centric ORB trailing exits are being labeled and surfaced correctly
- product-facing review can now happen from dashboard UI rather than raw curl output

## What This Does Not Prove

This run is not enough to prove:

- that `det_core_v2` is production-ready on a wider historical window
- that ORB-only trending behavior is robust across months and regimes
- that current deterministic defaults outperform the older baseline over a serious sample

So this is a clean verification run, not a final research verdict.

## Current Research Standard

Before deterministic replay research is called closed, we still need:

1. a wider replay window on current code
2. a named baseline run for comparison
3. comparison in `/strategy/evaluation`
4. strategy keep/tune/remove decisions from current evidence

## Current Recommended Workflow

1. Queue a new historical run over a wider deterministic v2 window.
2. Open `/strategy/evaluation`.
3. Compare the new run against a named baseline run.
4. Record conclusions in the research findings doc only after the run-scoped compare page confirms the deltas.

## Operator Shortcuts

Replay monitor:

- `/historical/replay`

Evaluation compare page:

- `/strategy/evaluation`

Core APIs:

- `/api/strategy/evaluation/summary`
- `/api/strategy/evaluation/trades`
- `/api/historical/replay/session`

## Relationship To Older Research

The older note in:

- [STRATEGY_RESEARCH_FINDINGS_2026-02-28.md](STRATEGY_RESEARCH_FINDINGS_2026-02-28.md)

is still useful as historical context, especially for why the older deterministic baseline was changed.

But for current replay-stack truth and current `det_core_v2` verification, use this document first.
