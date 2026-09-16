# strategy_platform — current strategy & config docs

The live strategy-platform references, as of 2026-09-16. (Older numbered docs
`00–05`, the dead config-consolidation plan/registry, superseded gate
write-ups, and — as of 2026-09-16 — the pre-Dhan-migration entry-pipeline and
deployment-state docs that used to be listed here, all moved to
[../archive/strategy_platform_old/](../archive/strategy_platform_old/); they
describe the Kite-era system and are historical only.)

| Doc | What |
|---|---|
| [CONFIG.md](CONFIG.md) | **Config — one source** (`.env.compose`) + switchable profiles + deploy. |
| [EXIT_SYSTEM.md](EXIT_SYSTEM.md) | The exit policy stack (`EXIT_STRATEGY_MODE`) — adaptive/lottery modes, no legacy inline exit path. |
| [DIRECTION_STRATEGY_SYNTHESIS.md](DIRECTION_STRATEGY_SYNTHESIS.md) | Direction: every proof + the regime-conditioned confluence council. Direction = the wall. |
| [OPPORTUNITY_GATE_DESIGN.md](OPPORTUNITY_GATE_DESIGN.md) | Selection Gate 1 — rank-relative-to-today + cost floor + budget (replaces the absolute ATR cliff). |

For the whole pipeline (how these fit together): **[../SYSTEM_FLOW.md](../SYSTEM_FLOW.md)**. For
current live/paper status per instrument, see the root **[README.md](../../README.md)**
(note its own known-stale banner) rather than anything in this folder or its archive.

## Core principles (unchanged)
1. **Loose coupling** — components talk over Redis/Mongo contracts, not direct calls.
2. **Config-driven** — behaviour changes via `.env.compose` (see CONFIG.md), never code edits.
3. **Sim must equal live** — identical code, ML versions, config.
4. **Everything traceable** — every decision writes a trace; if you can't explain a trade in 60s, that's a bug.
