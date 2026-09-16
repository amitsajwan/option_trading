# Product Closure Program

As-of date: `2026-04-04`

## Objective

Move the trading platform from "working engineering stack" to "polished, governed product" with:

- clean replay and research truth
- stable deterministic and ML runtime contracts
- trader-readable monitoring and evaluation
- explicit release gates
- clear ownership across product, architecture, trading, and engineering

This document defines the delivery program, not just the code backlog.

## Product Standard

The product is considered polished only when all of the following are true:

1. replay, evaluation, and UI all agree for the same `run_id`
2. production lanes are explicit and limited:
   - live: `ml_pure`
   - replay/research: `deterministic`
3. every strategy has:
   - a clear entry thesis
   - an owned exit thesis
   - capital-weighted evidence
4. monitoring is operator-readable:
   - no stale/mixed run confusion
   - exits and decision reasons are explained
5. runtime promotion is governed:
   - release criteria are written down
   - rollback path is explicit
   - owner approval is explicit

## Program Roles

### Program Manager

Owns delivery, sequencing, and closure.

Responsibilities:

- maintain the master plan and weekly status
- enforce entry/exit criteria for every workstream
- coordinate engineering, trading, and architecture decisions
- block incomplete releases from being called "done"

Authority:

- can stop release if a workstream misses acceptance criteria
- can force backlog cuts to protect schedule and product quality

### Product Owner

Owns what "good" means from user and operator perspective.

Responsibilities:

- define product surface expectations for dashboard, replay, and operator UX
- prioritize what must be polished for first trusted release
- own documentation completeness for user-facing and operator-facing flows

### System Architect

Owns structural integrity.

Responsibilities:

- protect lane separation between deterministic and `ml_pure`
- enforce loose coupling across regime/router/strategy/tracker/risk/evaluation
- review changes for contract drift, hidden coupling, and technical debt risk

### Lead Trader / Strategy Owner

Owns trading correctness.

Responsibilities:

- approve strategy thesis, invalidation, and exit behavior
- define what evidence is sufficient to keep, tune, or retire a setup
- sign off on strategy profile composition

### Quant Research Lead

Owns evidence quality.

Responsibilities:

- define replay windows and comparison methodology
- produce capital-weighted evaluation slices by strategy, regime, and exit type
- maintain research notes as current truth, not stale narrative

### Runtime Engineer

Owns engine/runtime behavior.

Responsibilities:

- strategy runtime, risk, tracker, router, regime logic
- runtime safety and session lifecycle behavior
- deterministic and `ml_pure` engine correctness

### Data / Persistence Engineer

Owns replay and storage correctness.

Responsibilities:

- Redis, Mongo persistence, run identity, dedupe, rerun correctness
- event integrity across votes, signals, positions, traces
- dataset consistency and backfill correctness

### Dashboard / UX Engineer

Owns operator-facing product quality.

Responsibilities:

- dashboard APIs, replay UX, explainability surfaces
- run-scoped views, readable labels, trustworthy panels
- remove ambiguity from operator workflow

### QA / Release Engineer

Owns acceptance and release safety.

Responsibilities:

- regression suite, smoke checks, release checklist
- VM redeploy validation
- versioned rollback instructions

## Recommended Team Shape

Minimum serious team:

- `1` Program Manager
- `1` Product Owner
- `1` System Architect
- `1` Lead Trader / Strategy Owner
- `1` Quant Research Lead
- `2` Runtime Engineers
- `1` Data / Persistence Engineer
- `1` Dashboard / UX Engineer
- `1` QA / Release Engineer

Lean mode can combine roles:

- Product Owner + Program Manager
- System Architect + Lead Runtime Engineer
- Trader + Quant Research Lead
- QA + Release Engineer

But the responsibilities must still exist, even if one person covers multiple roles.

## Workstreams

### Workstream A: Runtime Product Integrity

Owner:
- Runtime Engineer

Review:
- System Architect
- Lead Trader

Scope:

- deterministic router/profile correctness
- strategy-owned exits
- tracker/risk universal mechanics only
- runtime lane separation

Done when:

- default deterministic profile is documented and reproducible
- every active strategy has a clear thesis and owned exit
- no accidental live dependency on deterministic lane

### Workstream B: Replay and Evaluation Truth

Owner:
- Data / Persistence Engineer

Review:
- Quant Research Lead
- QA / Release Engineer

Scope:

- run-scoped persistence
- rerun correctness
- vote/signal/position/trace consistency
- summary/trades/session agreement

Done when:

- same `run_id` yields consistent counts across APIs and UI
- replay reruns do not silently reuse stale payloads
- research outputs are trustworthy enough for trader review

### Workstream C: Dashboard and Operator UX

Owner:
- Dashboard / UX Engineer

Review:
- Product Owner
- Program Manager

Scope:

- replay page clarity
- decision explainability
- exit labeling
- alert quality

Done when:

- an operator can explain what happened in one run without opening code
- panels do not mix data from unrelated runs
- labels reflect actual mechanisms, not approximations

### Workstream D: Research and Strategy Validation

Owner:
- Quant Research Lead

Review:
- Lead Trader
- Product Owner

Scope:

- capital-weighted replay comparisons
- regime and strategy contribution
- exit reason analysis
- strategy keep/remove/tune decisions

Done when:

- current default strategy profile is backed by current research
- stale research notes are retired or clearly marked historical
- production profile is justified with evidence

### Workstream E: Release Governance

Owner:
- QA / Release Engineer

Review:
- Program Manager
- System Architect

Scope:

- release checklist
- VM deployment guide
- rollback procedure
- smoke validation

Done when:

- every release has versioned steps
- rollback is tested and short
- runtime health checks are explicit

## Product Backlog By Priority

### P0: Must Close

- run-scoped replay/evaluation truth
- deterministic default profile documentation
- current research rerun on current code
- release checklist and rollback
- operator replay page trustworthy for one run

### P1: Must Improve Before Calling It Polished

- capital-weighted dashboard defaults
- strategy profile catalog with clear purpose
- removal or quarantine of weak experimental strategies
- operator playbook for alerts and exit reasons

### P2: Nice But Not Blocking Closure

- richer explainability narratives
- trend/exhaustion trader annotations
- profile comparison dashboard

## Acceptance Gates

### Gate 1: Engineering Closure

Required:

- tests green on touched modules
- no mixed-run corruption in replay surfaces
- deterministic and `ml_pure` lanes documented and separated

### Gate 2: Research Closure

Required:

- current replay on current router/default profile
- comparison vs prior baseline
- strategy/regime/exit contribution table

### Gate 3: Trader Closure

Required:

- trader review on each default playbook
- explicit keep/tune/remove outcome for every active deterministic strategy

### Gate 4: Product Closure

Required:

- dashboard/replay is understandable without code reading
- operator can diagnose a run from UI + docs
- no stale docs claiming current truth

### Gate 5: Release Closure

Required:

- deploy steps verified on runtime VM
- rollback steps written
- release owner named

## Current Program Status

### Closed Recently

- run-scoped summary/trades APIs
- rerun vote persistence fix
- replay trail labeling cleanup
- run-scoped historical session and deterministic diagnostics
- deterministic v2 router/profile cleanup

### Still Needed For Full Closure

- fresh wider-window deterministic v2 research
- explicit production readiness decision on deterministic default profile
- release checklist and rollback doc
- operator playbook for alerts and decision reasons
- final product surface review

## Weekly Operating Cadence

### Monday

- program review
- blocker review
- scope changes approved or rejected

### Wednesday

- research review
- trader review
- architecture review on risky changes

### Friday

- release readiness review
- doc completeness review
- decision log update

## Decision Log Format

Every major decision should be captured as:

- date
- owner
- decision
- alternatives considered
- evidence used
- rollback/revisit trigger

## Definition of Done

The product is done when:

- engineering says it is stable
- research says it is evidenced
- trader says it is sensible
- product says it is understandable
- release says it is deployable

If any one of those is missing, the platform is not closed.

## Immediate Next 5 Tasks

1. Run wider deterministic v2 research on current code, not historical notes.
2. Produce a formal strategy keep/tune/remove table for all default deterministic strategies.
3. Write release checklist and rollback runbook for dashboard, strategy runtime, and persistence services.
4. Write operator playbook for replay interpretation, alert handling, and exit reason reading.
5. Freeze the first trusted product baseline with named owners and sign-off.
