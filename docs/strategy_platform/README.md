# strategy_platform — current strategy & config docs

The live strategy-platform references. (Older numbered docs `00–05`, the dead
config-consolidation plan/registry, and superseded gate write-ups moved to
[../archive/strategy_platform_old/](../archive/strategy_platform_old/).)

| Doc | What |
|---|---|
| [CONFIG.md](CONFIG.md) | **Config — one source** (`.env.compose`) + switchable profiles + deploy. The config doc. |
| [DIRECTION_STRATEGY_SYNTHESIS.md](DIRECTION_STRATEGY_SYNTHESIS.md) | Direction: every proof + the regime-conditioned confluence council. Direction = the wall. |
| [OPPORTUNITY_GATE_DESIGN.md](OPPORTUNITY_GATE_DESIGN.md) | Selection Gate 1 — rank-relative-to-today + cost floor + budget (replaces the absolute ATR cliff). |

For the whole pipeline (how these fit together): **[../SYSTEM_FLOW.md](../SYSTEM_FLOW.md)**.

## Core principles (unchanged)
1. **Loose coupling** — components talk over Redis/Mongo contracts, not direct calls.
2. **Config-driven** — behaviour changes via `.env.compose` (see CONFIG.md), never code edits.
3. **Sim must equal live** — identical code, ML versions, config.
4. **Everything traceable** — every decision writes a trace; if you can't explain a trade in 60s, that's a bug.
