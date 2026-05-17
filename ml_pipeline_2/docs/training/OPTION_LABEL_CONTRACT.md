# Option-P&L Label Equivalence Contract

**Status:** v1.0.0 — drafted 2026-05-17, blocking gate before Step 2 (labeler).
**Config:** [`ml_pipeline_2/configs/research/option_label_contract.json`](../../configs/research/option_label_contract.json)
**Related:** [PROJECT_PLAN.md §15](../../../docs/PROJECT_PLAN.md) candidate #1; [JIRA_STAGED_RECIPE_RISK_BASIS_FIX.md](../JIRA_STAGED_RECIPE_RISK_BASIS_FIX.md).

---

## Purpose

When we train a model on option-P&L labels and deploy it, the runtime must execute trades **identically to how the labeler simulated them**. Every divergence is a silent edge killer:

- Labeler picks ATM=24500, runtime picks ATM=24600 → model is making predictions for a contract the runtime never trades.
- Labeler exits at close-of-bar-15, runtime exits at open-of-bar-16 → model's expected hold-P&L doesn't materialize.
- Labeler bakes in 200 bps cost, runtime fills at 50 bps → model under-fires; thresholds are wrong.

This document is the audit checklist. Every clause has a labeler call site, a runtime call site, and a divergence failure mode. **Both sides must read the same source field whenever possible** rather than re-computing.

---

## Why the snapshot is the natural single source of truth

The snapshot is already built upstream of both training and runtime. It carries:

- `atm_strike` — pre-computed via [`market_snapshot.py:_nearest_strike`](../../../snapshot_app/core/market_snapshot.py#L275)
- `expiry` — selected from the chain's `expiry` field via [`_option_expiry_date`](../../../snapshot_app/core/market_snapshot.py#L144)
- `strike_step()` — chain-specific
- The full chain (per-strike close, OI, IV, volume)

**Rule:** The labeler must read these from the snapshot it's labeling. The labeler does NOT recompute ATM. This makes ATM divergence structurally impossible.

The only fresh lookup the labeler performs that the runtime doesn't is the **future bars** (t₀+1 ... t₀+N) needed to compute the exit premium. The runtime experiences these one bar at a time; the labeler reads them all at once. That's fine — as long as the labeler uses the same "close of minute T" rule the runtime would use one bar at a time.

---

## Equivalence checklist

| # | Clause | Labeler reads | Runtime reads | Divergence failure |
|---|--------|---------------|---------------|---------------------|
| 1 | ATM strike | `snapshot.atm_strike` (no recompute) | `snap.atm_strike` ([option_selector.py:70](../../../strategy_app/engines/option_selector.py#L70)) | Different contract entirely → model is useless |
| 2 | Strike step (for OTM/ITM recipes) | `snapshot.strike_step()` | `snap.strike_step()` ([option_selector.py:120-122](../../../strategy_app/engines/option_selector.py#L120-L122)) | Wrong neighboring strike → wrong delta + IV |
| 3 | Expiry | `snapshot.expiry` (chain's published expiry) | Same — chain's expiry is what the runtime trades | Trading next-week's contract while label refers to this-week's |
| 4 | Entry premium | `options.close at (t₀, strike, expiry, side)` | Live broker fill at t₀ close in production; simulated close in replay | Label uses bar close but runtime fills next-bar open → ~1 bar of futures move worth of error |
| 5 | Exit premium (max hold) | `options.close at (t₀+N, same strike, same expiry, same side)` | Same — runtime closes at `entryIdx + max_hold_bars` close | Exit timing drift |
| 6 | Stop / target | Premium-relative: stop_pct / target_pct of `entry_premium`. Check each intermediate bar in [t₀+1, t₀+N], earliest hit wins. | Same loop in runtime exit-evaluator | If runtime checks only at end-of-hold and labeler checks per-bar → labels see optimistic exits |
| 7 | Cost | `TradingCostModel(brokerage=Rs.20/order, charges=2.5 bps/side, slippage=7.5 bps/side)` — imported, not re-derived | Same — `MemorySignalLogger` already uses this class ([test_research_cost_model.py:15](../../../strategy_app/tests/test_research_cost_model.py#L15)) | Optimistic labels, over-confident model |
| 8 | Risk basis | `option_premium` (set on recipe metadata) | Recipe `risk_basis=option_premium` enforced by [runtime_contract.py:57-61](../../../strategy_app/runtime_contract.py#L57-L61) guard | Underlying-basis stop on premium-basis recipe → instant stop-out on noise |
| 9 | Liquidity filter | Reject label if `entry_oi < 1000` OR `entry_premium < 5` | Runtime liquidity_gate_v1 must use same thresholds | Labels include rows runtime would never trade |
| 10 | Missing quote at any t | `skip_label` (do not emit, do not impute) | Runtime would never see this minute as tradeable | Imputed labels create phantom training signal |

---

## What changes outside the labeler

Concentrating the changes here so the audit is in one place:

### Recipe catalog
New stage-3 recipe entries for `ATM_CE_9`, `ATM_PE_9`, `ATM_CE_15`, `ATM_PE_15` (config under `recipes_v1`). Each MUST carry:

```json
{
  "risk_basis": "option_premium",
  "stop_loss_pct": 0.20,           // % of premium, not underlying
  "take_profit_pct": 0.30,
  "max_hold_bars": 9,
  "strike_rule": {"offset_steps": 0, "anchor": "atm_at_entry"}
}
```

### Runtime — `trade_signal_builder.py`
Already supports `risk_basis=option_premium` branch ([per JIRA fix doc](../JIRA_STAGED_RECIPE_RISK_BASIS_FIX.md)). Audit needed: confirm the premium-basis path uses the recipe's `stop_loss_pct` as % of premium, NOT of underlying.

### Runtime — `option_selector.py:select_strike`
Today's smart-strike logic can reject (IV too high) or shift to OTM_1 based on `confidence`. For v1, the labeler will use a deterministic strike-offset rule per recipe. **Decision: disable smart-strike's adaptive behavior for these recipes.** Either:
- (a) Set `STRATEGY_SMART_STRIKE_ENABLED=0` → falls back to legacy_atm, matching the labeler's ATM-at-entry rule.
- (b) Add a per-recipe field `disable_smart_strike: true` honored by the selector.

Recommend (b) — leaner blast radius.

### Dashboard — `pnl_pct` interpretation
Trade rows currently render `pnl_pct` as % of underlying entry. For `risk_basis=option_premium` recipes, render % of premium and tag the basis:

```
50100 CE · entry 120 → exit 132 · +10.0% (premium)
```

vs. current futures-style:

```
LONG · entry 50100 → exit 50161 · +0.12% (underlying)
```

Two-line render or `pnl_pct_basis: "option_premium"` tag with conditional formatting.

---

## What does NOT change

- Snapshot schema
- Stage 1 / Stage 2 / Stage 3 feature views (`stage1_entry_view_v2`, etc.)
- HPO / CV / publication gates (the trading-utility evaluator will just see different P&L because labels are different)
- Persistence (JSONL + Mongo schemas already record strike + option_type + premium fills)
- Live ↔ Replay equivalence contract (same engine, same snapshots — premium-basis recipes work in both)
- The new Diag tab and per-minute decisions timeline (entry_prob just means something different)

---

## Sanity gates (Step 3, must run before Step 4 training)

The labeler's output must pass these BEFORE we start any HPO run. Thresholds from `option_label_contract.json::sanity_report_gates`:

| Check | Pass band | Fails if |
|-------|-----------|----------|
| Label positive rate per recipe | 5% – 60% | <5% (too rare to learn) or >60% (trivial / leaky) |
| Avg entry premium | ≥ 5 | Tiny-premium trap |
| Missing-quote rate | ≤ 30% | Sparse data — model would learn from skewed sample |
| Win rate before cost vs after cost | Cost gap ≤ 30 pp | Otherwise the label is dominated by cost noise |
| Label distribution by expiry-day | No expiry-day-only edge | Edge concentrated in 1-2 days = lottery, not generalization |
| Label distribution by time-of-day | No 9:15-9:20 spike | Open-noise label, won't generalize |

If any gate fails: investigate, do not train.

---

## Open questions deferred to v2

These don't block v1 but are worth tracking:

1. **Slippage model** — v1 uses flat 0.5 pt + 200 bps. Real slippage is OI-dependent. v2 could regress slippage on OI.
2. **Multi-strike recipes** — "Best of ATM, ATM±1": tempting but creates leakage risk if the "best" pick uses post-hoc info. Defer.
3. **Greek-aware labels** — Predict P&L of an iron condor or risk-reversal rather than a single leg. Big code lift; v3.
4. **Forward-data integration** — Once 4-6 weeks of post-Monday Kite shadow data exists, re-run v1 labeler over that window as a held-out OOS check.

---

## Sign-off checklist before unblocking Step 2 (labeler implementation)

- [ ] Contract config reviewed and merged
- [ ] Audit row #4 (entry premium = close-of-t₀) confirmed in `trade_signal_builder.py` runtime path
- [x] Audit row #7 (cost model) — **VERIFIED 2026-05-17**: canonical cost class is `strategy_app.tools.offline_strategy_analysis.TradingCostModel` (defaults: Rs.20/order brokerage, 2.5 bps/side charges, 7.5 bps/side slippage). Labeler must import this class, not re-derive a flat rate. `MemorySignalLogger` already uses it ([test_research_cost_model.py:15](../../../strategy_app/tests/test_research_cost_model.py#L15)). The initial contract draft's "200 bps + 0.5 pt" was wrong for option labels — corrected.
- [ ] Recipe catalog v1 entries drafted (4 recipes)
- [ ] Smart-strike per-recipe disable flag designed (option b)
- [ ] Dashboard `pnl_pct_basis` plumbing scoped (separate ticket)
