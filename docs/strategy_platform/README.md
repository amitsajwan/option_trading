# Strategy Platform — Handover Documentation

*Owner handover pack. Read in order. No code lives here — these are the design,
analysis, backlog, and architecture docs for the next team.*

---

## What this is

An automated intraday BANKNIFTY options trading system. It ingests live Kite data,
builds market snapshots, runs a strategy engine that decides CE/PE entries, manages
positions with composable exit policies, and (optionally) routes orders to the broker.

Two exit *philosophies* now exist and are config-selectable:

- **Scalper** — many trades/day, capture small consistent gains, tight exits.
- **Lottery** — few high-conviction trades, let winners run, lose small often.

A **daily ops tool** (the ⚙ OPS drawer in the dashboard) lets an operator change
config and replay *today* through the engine, comparing actual vs simulated results.

---

## Reading order

| # | Doc | For |
|---|---|---|
| 00 | [Context & Findings](00_CONTEXT_AND_FINDINGS.md) | Everyone — what's been learned, the data, the open questions |
| 01 | [Architecture](01_ARCHITECTURE.md) | Engineers — how the system is wired, loose-coupling + config-driven principles |
| 02 | [Multi-Day Sim](02_MULTI_DAY_SIM.md) | The immediate next project — full spec, logic, stories, DoD |
| 03 | [Multi-Strategy Platform](03_MULTI_STRATEGY_PLATFORM.md) | The future vision — scalp + lottery + N strategies concurrently, container topology |
| 04 | [Backlog](04_BACKLOG.md) | Tech lead / PM — consolidated epics, stories, tasks, Definition of Done |
| 05 | [Config Reference](05_CONFIG_REFERENCE.md) | Operators + engineers — every env var, grouped, with defaults and meaning |

---

## Core principles (do not violate)

1. **Loose coupling.** Components talk over Redis pub/sub and JSONL/Mongo contracts,
   not direct calls. A new strategy or consumer subscribes; it doesn't get wired in.
2. **Config-driven.** Behaviour changes via env vars, never code edits. If you find
   yourself editing code to change a threshold, that threshold belongs in config.
3. **Sim must equal live.** A simulation is only trustworthy if it uses the identical
   code, identical ML library versions, and identical config as live. See the sim
   fidelity rules in [Architecture](01_ARCHITECTURE.md).
4. **Everything traceable.** Every decision writes a trace. If you can't explain why a
   trade fired in 60 seconds from the trace, that's a bug.

---

## Status at handover

- Scalper exit stack: **built, tested (30 unit tests), live-capable, currently live in paper.**
- Lottery exit mode: **built, tested, sim-validated on one day. NOT live.**
- OPS daily-sim tool: **built and deployed; verified accurate to the decimal vs raw option prices.**
- Execution bridge (paper/kite/shadow adapters): **built, paper live, kite/shadow unverified against real funds.**
- Multi-day sim: **NOT built — this is project #02.**
- Multi-strategy/multi-container: **design only — project #03.**

See [Context & Findings](00_CONTEXT_AND_FINDINGS.md) for the evidence behind each.
