# Entry Model v3 — Cost-Aware Option-P&L Label (Spec)

**Status:** proposed — ready to implement · **Date:** 2026-06-05 · **Owner:** ML (Claude)
**Supersedes the label of:** [ENTRY_MODEL_V2_PUBLISHED.md](ENTRY_MODEL_V2_PUBLISHED.md)
**Context:** system is **HALTED** after a real −6.1% day ([[project_hardstop_failure_2026-06-05]]); first real trade netted ≈ −₹46 on a "win" ([[project_first_real_trade_2026-06-05]]).

---

## 1. The problem (why v2's label is wrong)

v2 trains on *"did the index move ≥0.10% (~54 pts) in 5 min."* What 54 pts does to the option you buy:

- ATM delta ≈0.5 → 54 idx pts → ~27 pts premium → on ~950 premium ≈ **2.8% gross**.
- Measured round-trip cost today: slippage ~1.0% + charges ~0.3% = **~1.3%**.
- A *perfectly* predicted, *fully* captured 54-pt move nets ~1.5% — before theta, before being wrong on direction, before exiting early at 5 bars. **Realized ≈ flat.**

So v2 optimizes a target that sits **at the cost floor**. Even its wins don't pay — exactly the scratch-flat + cost-bleed seen live. This is the documented "label ceiling" ([[project_overfit_2024_finding]] → [[project_option_pnl_breakthrough]]): the edge is in **option P&L after cost**, not index points.

**The training cost assumption made it invisible:** every staged config uses `cost_per_trade=0.0006` (6 bps) — ~20× below the real ~130 bps. At 6 bps nearly any move looks profitable.

## 2. Prior art — read before hoping (do not relearn)

- **B1 (2026-05-15):** the C1 recipe re-run at `cost_per_trade=0.02` (200 bps) → **HELD, block_rate=1.0** — no entries cleared cost ([MODEL_STATE_20260515.md](../ml_pipeline_2/docs/training/MODEL_STATE_20260515.md)).
- The entire long-option-buy arc (C1→E8, rules) produced **zero verified-OOS edge**; verdict: *"long-ATM-1-min lane is exhausted"* ([[project_e7_oos_result_2026-05-25]], [[project_rules_verdict]], [[project_e8_oos_failure_2026-05-25]]).
- "Direct option-P&L label" is **next-experiment candidate #1** in PROJECT_PLAN §15, deferred until fresh data existed. Fresh 2026 data + real measured cost now exist → conditions are met.

**Honest expectation:** the strong prior is this label *confirms* long-ATM entry can't clear ~130 bps at short horizons, rather than creating new edge. That is still a decisive, valuable result — and unlike B1 this design is **direction-agnostic** and **sweeps horizon**, so it answers the real question: *does ANY (horizon × move-size) long-option entry clear real costs — and if not, we pivot (sell-side / different instrument), we don't keep tuning entry.*

## 3. The label

Use the **existing recipe option-P&L oracle** (`staged/pipeline.py::_build_oracle_targets`), which books real CE/PE premium path returns per recipe (TP/SL/horizon) and subtracts `cost_per_trade`.

```
option_net(bar, recipe, side) = option_path_return(recipe, side) − cost_per_trade
best_net(bar)                 = max over recipes × {CE,PE} of option_net   # direction-agnostic
entry_label(bar)              = 1 if best_net(bar) ≥ WIN_THRESHOLD
```

- **Direction-agnostic** (`max` over CE/PE): Stage-1 stays a *timing* gate ("is there a long-option trade now that clears cost"); the side is the direction model's job. This is the key difference from B1.
- **Labeler:** `recipe_best_positive_v1` already computes `best_net_return_after_cost`. Add a `win_threshold` knob (default 0.0 → ">0 after cost"; sweep up).
- Reuses `stage3_recipe_view_v2` (option path returns precomputed) — no new ETL.

### 3.1 Realistic cost — the central change
Set **`cost_per_trade = 0.013`** (measured round-trip: ~1.0% slippage + ~0.3% charges). Sweep `{0.010, 0.013, 0.020}` to bracket measured + B1's 200 bps.

### 3.2 Win threshold sweep
`WIN_THRESHOLD ∈ {0.00, 0.05, 0.08}` — "clears cost", "≥5% net (buffer for losers)", "≥8% net". Higher threshold = rarer, higher-conviction, but must keep positive rate learnable (≥ ~10%).

### 3.3 Horizon — confront the theta tension
The recipe catalog `fixed_l0_l3_v1` already books trades at **15–20 min** (TP 0.20–0.25% / SL 0.08–0.10% underlying) — longer than v2's 5 min, giving the option room to overcome theta. Also evaluate the `midday_l3_adjacent_v1` catalog (15–25 min). **Do not force 5 min** — the exits cutting at 5 bars are part of what made v2 unprofitable; the label should reflect a realistic hold that can actually clear cost.

## 4. Features, model, calibration
- Features: `fo_velocity_v1` (51) — unchanged; the label changes, not the inputs.
- Model + HPO: same staged search (xgb/lgbm family, brier objective), isotonic calibration (reuse `publish_entry_v2_calibrated.py`).

## 5. SHIP GATES (cost-aware — the whole point)

A model ships only if, on the **OOS holdout (2024-08→10)** at the chosen operating threshold:

1. **Positive rate** ∈ [0.10, 0.45] on train (label must be learnable, not collapsed like B1).
2. **Beats cost (the gate):** mean `best_net` of FIRED bars **> 0 with margin**, and cost-aware **profit factor ≥ 1.3** (gross/|loss| after 130 bps). This is the gate B1 failed.
3. **Separation:** E[best_net | fired] − E[best_net | not-fired] ≥ +1.0% (fires on the profitable subset, declines the rest).
4. Discrimination ROC-AUC ≥ 0.60; calibration ECE ≤ 0.05; drift ≤ 0.08.

**If no (cost × threshold × horizon) cell passes gate #2 → the result is the finding:** long-ATM buying does not clear real costs; stop tuning entry and pivot. Document and close the lane.

## 6. Run plan (ML VM)
1. Recreate VM (`ops/gcp/create_training_vm.sh`; 100 GB disk override — SSD quota).
2. **Ceiling diagnostic first (cheap, gates everything):** on `stage3_recipe_view_v2`, compute `best_net` at cost ∈ {0.010, 0.013, 0.020} across both recipe catalogs; report positive rate at each `WIN_THRESHOLD` AND the realized net-P&L distribution of the top-decile bars. *If even the best bars don't clear cost, stop here — no model can fix a target with no signal.*
3. If signal: sweep cost × threshold × catalog, HPO, calibrate, validate on §5 gates.
4. Publish only a model that clears gate #2; else write the negative-result doc.

## 7. Open decisions
- Cost model: flat 130 bps vs premium-dependent (cheaper options cost more in %). Start flat 130 bps; refine if a cell passes.
- Keep direction-agnostic `max(CE,PE)` label vs per-side. Start agnostic (timing only).
- System stays **HALTED** until a model passes gate #2 in sim.
