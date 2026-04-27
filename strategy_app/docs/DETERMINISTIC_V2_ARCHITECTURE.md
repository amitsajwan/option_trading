# Deterministic V2 Architecture

Last reviewed: 2026-04-27

## Goal

Keep the deterministic engine trader-like in behavior while preserving clean system boundaries.

The design principle is:

- regime decides whether the market is worth trading
- router decides which playbooks are allowed
- strategy owns its thesis and its thesis-specific exit
- tracker owns only universal mechanics
- risk manager sizes and halts
- evaluation judges on capital-weighted outcomes

## Ownership Model

### Entry Ownership

Each entry comes from one named strategy. Strategies present in the codebase:

- `ORB`
- `OI_BUILDUP`
- `VWAP_RECLAIM`
- `PREV_DAY_LEVEL`
- `HIGH_VOL_ORB`

The **production default profile** (`det_prod_v1`) uses a narrower set:

| Regime | Strategies |
|---|---|
| `TRENDING` | `IV_FILTER`, `ORB`, `OI_BUILDUP` |
| `SIDEWAYS` | `IV_FILTER`, `OI_BUILDUP` |
| `EXPIRY` | `IV_FILTER` |
| `PRE_EXPIRY` | `IV_FILTER`, `ORB`, `OI_BUILDUP` |
| `HIGH_VOL` | `IV_FILTER`, `HIGH_VOL_ORB` |
| `AVOID` | — |

`VWAP_RECLAIM` and `PREV_DAY_LEVEL` are available but are not in `det_prod_v1`. They appear in `det_core_v2` (research/comparison profile).

The default router no longer allocates new `TRENDING` entries to `EMA_CROSSOVER`.

### Exit Ownership

Open positions are evaluated first by the owning strategy.

Cross-strategy exits are allowed only through explicit helper mappings in the router.

If no owner/helper exit strategy is available, the router falls back to the legacy universal exit pool.

This keeps attribution clean while still preserving backward compatibility.

### Universal Exits

These remain outside strategy ownership:

- hard stop
- trailing stop
- time stop
- global risk halt / risk breach

Those are enforced by `PositionTracker`.

## Default Behavioral Changes

### Router

- `EMA_CROSSOVER` removed from the default `TRENDING` entry set
- `EMA_CROSSOVER` removed from the default universal exit pool

### Regime-Shift Handling

Defaults now assume regime-shift exits are noisy and should be confirmed:

- `regime_shift_confirm_bars = 2`
- `regime_shift_min_profit_hold_pct = 0.08`

This reduces whipsaw exits without changing hard risk mechanics.

### Strategy Tightening

`ORB`
- exit now requires a small buffer through the opening-range boundary, not a one-tick reversal

`OI_BUILDUP`
- exit now requires a small non-zero `r5m` reversal threshold
- exit now enforces a minimum hold before regime-shift exit can fire

`PREV_DAY_LEVEL`
- now owns a thesis exit via prior-day level re-entry
- no longer depends on ORB as an implicit exit owner

## Review Checklist

When reviewing deterministic strategies, ask:

1. Does the strategy represent a distinct playbook?
2. Does it have a trader-readable invalidation condition?
3. Is the exit owned by the same thesis that opened the trade?
4. Are universal exits reserved for truly universal mechanics only?
5. Are results judged on capital-weighted contribution, not just raw option return?

## Next Refactor Targets

1. Re-run research on the current router and current defaults.
2. `VWAP_RECLAIM` is not in the production default (`det_prod_v1`); it remains in `det_core_v2` for research comparison. Decide whether it warrants its own profile entry or stays research-only.
3. A profile system already exists (`det_prod_v1`, `det_core_v2`, `det_setup_v1`, `det_v3_v1`). Consider formalizing profile purpose documentation:
   - `det_prod_v1`: conservative core (production)
   - `det_core_v2`: broader research/comparison
   - `det_setup_v1`, `det_v3_v1`: trader composite variants
4. Move evaluation dashboards to capital-weighted defaults.
