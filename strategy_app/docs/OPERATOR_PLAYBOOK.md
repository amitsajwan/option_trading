# Operator Playbook

As-of date: `2026-04-04`

## Purpose

This playbook explains how to read the replay and monitoring product surfaces without inspecting code first.

Use this with:

- [PRODUCT_CLOSURE_PROGRAM.md](PRODUCT_CLOSURE_PROGRAM.md)
- [CURRENT_TREE_VALIDATION.md](CURRENT_TREE_VALIDATION.md)
- [DETERMINISTIC_V2_ARCHITECTURE.md](DETERMINISTIC_V2_ARCHITECTURE.md)

## Core Rules

1. Trust `run_id`-scoped views over date-only views.
2. Trust `Range Trades` and summary before votes.
3. Read `exit_reason` together with `exit_mechanism`.
4. Treat stale data alerts as monitoring degradation, not automatic strategy failure.

## Historical Replay Workflow

### Step 1: Confirm scope

Check:

- `Latest Run ID`
- selected date range
- run note on the replay page

If the run is wrong, every downstream interpretation is wrong.

### Step 2: Confirm replay health

Check:

- replay status
- emitted event count
- collection counts
- whether the page is bound to the intended `run_id`

### Step 3: Read the product in this order

1. `Range Trades`
2. `Latest Closed Trade`
3. `Recent Signals`
4. `Recent Votes`
5. `Decision Diagnostics`

That keeps realized behavior ahead of candidate noise.

## How To Read Exits

### `TRAILING_STOP`

This is only the top-level reason. Use `exit_mechanism` to know the real trail owner.

Possible mechanisms:

- `GENERIC_TRAIL`
- `ORB_TRAIL`
- `OI_TRAIL`

### `REGIME_SHIFT`

Interpret as:

- thesis cracked
- exit logic too sensitive
- or regime confirmation too weak

### `TIME_STOP`

Interpret as:

- trade did not finish naturally within allowed time

### `STOP_LOSS`

Interpret as:

- hard protection activated

Then inspect:

- stop placement
- lot sizing
- clustering by strategy/regime

## How To Read Alerts

### `data_stale`

Meaning:

- current monitoring freshness is degraded

Operator response:

- do not trust fresh operational inference until stream health is checked

### `ml_pure_monitoring_unavailable`

Meaning:

- ML monitoring inputs are incomplete

Operator response:

- do not treat it as a model failure
- do treat it as a monitoring gap

### `risk_halt` / `risk_pause`

Meaning:

- risk layer is controlling participation

Operator response:

- inspect drawdown and risk limits before any override decision

## Minimum Clean-Run Checklist

- correct `run_id`
- correct date range
- non-zero emitted events
- summary/trades/session agreement
- no mixed profile leakage
- readable exit mechanism
- understandable strategy/regime contribution

If any of these fail, the run is not clean enough for decision-making.
