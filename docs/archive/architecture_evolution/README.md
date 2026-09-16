# Architecture Evolution

This folder contains the forward-looking architecture work for the BankNifty Options Algo platform.

## Goal

Move from the current tightly-coupled, partially pub/sub system to a clean **dual-layer event architecture**:

- **Durable layer** — Redis Streams for all processing pipelines (no message loss, replay, fault tolerance)
- **Notification layer** — Redis Pub/Sub for live UI display only (current state, low latency)

## Documents

| File | Purpose |
|---|---|
| [PLAN.md](PLAN.md) | Master implementation plan — epics, stories, acceptance criteria, sequencing |
| [CURRENT_STATE.md](CURRENT_STATE.md) | Honest snapshot of what exists today, what works, what is broken |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | Target state diagram and design decisions |
| [DECISIONS.md](DECISIONS.md) | Architecture Decision Records (ADRs) — why we chose X over Y |
| [PERFORMANCE_AND_STORAGE.md](PERFORMANCE_AND_STORAGE.md) | Performance analysis, JSONL assessment, health metrics per story |

## Status

> **Phase: Planning Complete** — all documents written and architect-reviewed. No code changed yet.
> Branch `arch/streams-loose-coupling` exists and ready. Work begins on owner's go.

## Guiding Principles

1. **No backward steps** — every change must be a strict improvement
2. **Incremental, deployable** — each story ships independently behind a feature flag
3. **Delete complexity** — `ConsumerLock` and pub/sub snapshot delivery go away entirely
4. **Consistent model** — sim already uses Streams correctly; live/OOS should match
5. **UI stays decoupled** — browser never reads from a durable stream directly; always via the notification layer
