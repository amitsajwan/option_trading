# Docs — index

**Start here → [SYSTEM_FLOW.md](SYSTEM_FLOW.md)** (the whole pipeline: gates, flow,
councils, config, current state, code map).

Everything in this top level is **current and authoritative**. Historical day-by-day
work, superseded specs, and old experiments live in **[archive/](archive/)** (kept for
analysis, not deleted).

---

## Understand the system
| Doc | What |
|---|---|
| **[SYSTEM_FLOW.md](SYSTEM_FLOW.md)** | **The map** — per-bar flow, every gate, the two councils, exit, persistence, verdict |
| [ENGINE_DECISION_FLOW.md](ENGINE_DECISION_FLOW.md) | Live-engine decision detail (which path is live, dead-code traps) |
| [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md) | Current state + honest strategy verdicts |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System architecture |
| [SYSTEM_FLOW_DIAGRAMS.md](SYSTEM_FLOW_DIAGRAMS.md) | Mermaid lane diagrams (training / live / replay) |
| [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md) | Containers / processes / topics |
| [UI_ARCHITECTURE.md](UI_ARCHITECTURE.md) | Dashboard / UI |

## Strategy detail
| Doc | What |
|---|---|
| [strategy_platform/DIRECTION_STRATEGY_SYNTHESIS.md](strategy_platform/DIRECTION_STRATEGY_SYNTHESIS.md) | Direction: proofs + the regime-conditioned council (the wall) |
| [strategy_platform/OPPORTUNITY_GATE_DESIGN.md](strategy_platform/OPPORTUNITY_GATE_DESIGN.md) | Selection Gate 1 (rank-relative-to-today + cost floor) |

## Config & operate
| Doc | What |
|---|---|
| [strategy_platform/CONFIG.md](strategy_platform/CONFIG.md) | **Config — one source** (`.env.compose`), profiles, deploy |
| [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md) | Go-real checklist |
| [RUNTIME_STATE_AND_RECOVERY.md](RUNTIME_STATE_AND_RECOVERY.md) | Runtime state + recovery |
| [OBSERVABILITY_GUIDE.md](OBSERVABILITY_GUIDE.md) | Logs / metrics / what to watch |
| [runbooks/](runbooks/) | Step-by-step ops (deploy, live cutover, sim replay, recovery, training release) |
| [TEAM_ONBOARDING.md](TEAM_ONBOARDING.md) | New-joiner onboarding |

## Models
| Doc | What |
|---|---|
| [MODELS_INDEX.md](MODELS_INDEX.md) | Model registry (what's trained / published / live) |
| [MODEL_OUTPUT_CONTRACT.md](MODEL_OUTPUT_CONTRACT.md) | Model bundle / output contract |

> ML training pipeline docs live with the code: `ml_pipeline_2/docs/`.

---

## Reading order for a newcomer
1. [SYSTEM_FLOW.md](SYSTEM_FLOW.md) — how a bar becomes a trade.
2. [strategy_platform/CONFIG.md](strategy_platform/CONFIG.md) — how to change/deploy config.
3. [strategy_platform/DIRECTION_STRATEGY_SYNTHESIS.md](strategy_platform/DIRECTION_STRATEGY_SYNTHESIS.md) — why direction is the hard part.
4. [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md) — what's actually true today.
5. [runbooks/](runbooks/) — when you need to operate it.
