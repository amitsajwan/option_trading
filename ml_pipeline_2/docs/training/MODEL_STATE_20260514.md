# ML Model State — 2026-05-14

> **Canonical snapshot for this research iteration.**
> Resuming the E2 launch that was queued on 2026-05-02 but never executed.
> Twelve-day gap was infrastructure/UI work, not training.

---

## 1. Why This Session Exists

E2 (`staged_dual_recipe.deep_hpo_e2_volatile_only.json`) was declared ready-to-run on
2026-05-02 with two pipeline fixes committed:

- **Fix 1** (`pipeline.py:3406-3418`) — enrich stage2 frame with `ctx_regime_*` from `snapshots_ml_flat`
- **Fix 2** (`pipeline.py:3789-3804`) — exclude SIDEWAYS from holdout block_rate

It was never launched. The intervening commits (`d09cb86`, `da81851`, `13d7fde`, `f578435`) show the team was prepping E2 for a new ML VM after the GCP project migration to `algo-trading-496203`, hit a `stage1_reuse` path bug, fixed it, then pivoted to infrastructure and UI work.

**Inherited state from 2026-05-02:**
- Live model: `staged_deep_hpo_c1_base_20260429_040848` (unchanged)
- `regime_gate_v1` active — only VOLATILE+SIDEWAYS sessions trade live
- D2 HELD: PF 1.194, MDD 29.5%, block_rate 3.97%
- E1 failed (pipeline bug, fixed)
- E2 config corrected for cross-platform paths (`stage1_reuse` removed)

---

## 2. What Happened 2026-05-02 → 2026-05-14 (Non-Training Work)

No training was run. The work was operational:

| Date | Area | Outcome |
|------|------|---------|
| Early May | GCP migration | New project `algo-trading-496203`, terraform config rewritten, ML VM and runtime VM provisioned |
| Early May | E2 config cross-platform fix | `stage1_reuse` removed (Windows-absolute ancestor path issue) |
| Early May | Publish gates | E2 publish gates relaxed (commit `f578435`) |
| 2026-05-13 | Snapshot pipeline | 2024 BankNifty futures parquet rebuilt on ML VM, published to GCS, synced to runtime VM |
| 2026-05-13 | Runtime VM bootstrap | Docker compose `historical` profile started; `phase1_market_snapshots_historical` populated |
| 2026-05-13/14 | Webapp refactor | Bloomberg-dark terminal UI; dead code stripped; Flow-3 button removed; chart auto-fit fix |
| 2026-05-14 | Runtime ops | Live guard JSON malformed-key fix; stage1/2/3/market_base parquet synced to runtime; historical replay tmux running at 1000× |

**Current operational state:**
- Historical replay covering 2024-01-01 → 2024-10-31 streaming into MongoDB at 1000×
- Replay UI shows trades from `strategy_app_historical` (Flow 2 — same ML engine as live, fed historical bars)
- ML VM idle — no training process running

---

## 3. Decision: Launch E2 Now

**Hypothesis (unchanged from 2026-05-02):** Training S2 only on VOLATILE+SIDEWAYS rows (8,942 samples) will produce a sharper CE/PE decision boundary where the edge actually lives. With Fix 2, the combined holdout gates are now measured on non-SIDEWAYS rows only — matching runtime behavior with `regime_gate_v1` active.

**Why E2 still makes sense after 12-day delay:**
1. No alternative experiment has been designed in the interval — E2 remains the highest-leverage next step in the documented research arc.
2. The infrastructure work clears the path: ML VM has all parquet views (`snapshots`, `market_base`, `stage1_entry_view`, `stage2_direction_view`, `stage3_recipe_view`), config bug fixed.
3. C1 has been live and shadow-trading since 2026-04-29 — almost two weeks of paper-mode data. The shadow telemetry should inform whether E2's hypothesis (VOLATILE-only training) is consistent with observed live behavior. (Out of scope for this session — flag for follow-up.)
4. The historical replay we just launched at 1000× will produce a parallel empirical baseline of C1's 2024 behavior over the next ~80 min — directly comparable to whatever E2 produces.

**Out-of-scope but flagged for future sessions** (per current bottleneck analysis):

- **Grid F — PRE_EXPIRY isolation.** D2 lifted TRENDING 0.306 → 1.195 (huge), but PRE_EXPIRY at 0.981 still drags combined PF. No grid has yet targeted PRE_EXPIRY label semantics.
- **Recipe set reduction.** `fixed_l0_l3_v1` (4 recipes) beat 7-recipe catalog. 2- or 3-recipe variants untested.
- **Stage 3 OVR replacement.** Stage 3 OVR signal absent in every run to date; always falls back to fixed recipe. A regime-aware fixed rule could replace OVR entirely.

---

## 4. E2 Run Plan

**Config:** `ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json`

**Key parameters (unchanged from 2026-05-02):**
- `stage2_decisive_move_filter.allowed_regimes: ["VOLATILE", "SIDEWAYS"]`
- `stage2_decisive_move_filter.min_ce_pe_edge: 0.002`
- Stage 1: 7 feature sets × 8 models × 3 HPO trials
- Stage 2: 1 feature set (`fo_midday_time_aware_plus_oi_iv`) × 10 models × 12 HPO trials, max 80 experiments / 4h
- Stage 3: 3 feature sets × 7 models × 8 HPO trials, max 50 experiments / 2h
- Recipe catalog: `fixed_l0_l3_v1`
- Training window: 2020-08-03 → 2024-04-30; valid: 2024-05–07; holdout: 2024-08–10

**Expected runtime:** 6-8 hours on `n2-standard-8`.

**Success criteria (relaxed gates per `f578435`):**
- VOLATILE PF ≥ 1.3
- combined PF ≥ 1.5 (now measured on non-SIDEWAYS holdout)
- block_rate ≥ 25% (on non-SIDEWAYS holdout)
- MDD ≤ 10%
- side_share CE ≥ 30%, PE ≥ 30%

**Outcomes:**
- If E2 passes all gates → publish, replace C1 as default.
- If VOLATILE PF ≥ 1.3 but other gates fail → force-deploy with `regime_gate_v1` (C1 pattern) and document gate-failure regime breakdown.
- If VOLATILE PF < 1.3 → strong evidence VOLATILE-only training does not improve the boundary; move to Grid F (PRE_EXPIRY targeted).

---

## 5. Run History (This Session)

| Date | Run ID | S1 ROC | S2 ROC | Trades | PF | MDD | Block | Outcome |
|------|--------|--------|--------|--------|----|-----|-------|---------|
| 2026-05-14 | `staged_deep_hpo_e2_volatile_only_20260514_161109` | 0.619 | **0.535** | 2,496 | **0.263** | **81.1%** | 88.5% | ❌ HELD — gates failed on PF, MDD, S2 ROC, S3 non-inferiority, net_return |

## 6. E2 Outcome Analysis

**The VOLATILE-only training hypothesis is disproven.** E2 made things *worse* than C1 across the board:

| Metric | C1 (live) | E2 | Δ |
|---|---|---|---|
| Stage 2 ROC | 0.591 | 0.535 | **−0.056** (toward random) |
| Combined holdout PF | 0.614 | 0.263 | **−0.351** |
| VOLATILE PF | 1.314 | 0.488 | **−0.826** (the regime we restricted to got WORSE) |
| TRENDING PF | 0.306 | 0.260 | −0.046 |
| PRE_EXPIRY PF | 0.178 | 0.252 | +0.074 |
| SIDEWAYS PF | 4.535 | **0.000** (167 trades, 0% wins) | **broken** |
| Max drawdown | (acceptable) | 81.1% | catastrophic |

**Mechanism (hypothesis):** Restricting Stage 2 training to VOLATILE+SIDEWAYS (~8.9k rows, ~24% of the original 37k) destroyed generalization. The model learned features that are only valid under the regime filter; at inference, the runtime regime label has measurement noise, and any mislabel pushes the input into an unseen region. Result: near-random predictions on the held-out test set. The SIDEWAYS collapse (0% win rate over 167 trades) is the signature of this — Stage 2 produces direction predictions that are reliably *wrong*.

## 7. Decision: NOT Grid F as scoped — Stage-1-only ablation first

The originally-queued Grid F (PRE_EXPIRY-isolated training) would repeat E2's architectural mistake with a different filter and an even smaller sample (~5k rows). The bottleneck is **NOT** Stage 2 sample purity. Filtering harms generalization.

Looking at the full Grid A→E arc, **D2 had the best Stage 2 to date** (ROC 0.618, VOLATILE PF 1.452). D2's failure was the combined cascade — block_rate too low (3.97% vs ≥25% gate) and PRE_EXPIRY+UNKNOWN dragging MDD to 29.5%. **D2 was held for runtime-gating reasons, not training reasons.**

### Next experiment: Stage-1-only ablation (cheap, high-info)

Run a replay over 2024 Jan-Oct with `strategy_app_historical` configured to bypass the Stage 2 directional gate and Stage 3 recipe selection. Use Stage 1 as a pure entry filter with a higher threshold:

```bash
STRATEGY_ML_PURE_BYPASS_GATES=1
STRATEGY_MIN_CONFIDENCE=0.55  # ~Stage 1 alone
```

Stage 1 published metrics are: PF=3.99, win=66.5% on validation (selected_threshold=0.5, 22k trades / 23k rows). The hypothesis: the directional cascade is *value-destructive* — Stage 1 alone with a simple directional heuristic (e.g. session momentum sign, or pure CE-bias since training distribution was PE-dominant) may match or beat the full cascade.

**Why this beats Grid F:**
- **No retraining cost.** Just a config change + a ~80-minute replay at 1000×.
- **Tests a fundamental hypothesis:** that the cascade hurts. If proven, every directional-gate experiment to date has been chasing the wrong goal.
- **Falsifiable in one run:** if PF stays below 1.0 with bypass, the cascade is necessary and we explore D2-revisit (option B below).

### Fallback if Stage-1-only fails: D2-revisit with runtime PRE_EXPIRY block

Take **D2's exact config** (`staged_deep_hpo_d2_high_edge_20260501_040643`) and:
- Add `PRE_EXPIRY` and `UNKNOWN` to `runtime.prefilter_gate_ids` alongside `regime_gate_v1` 
- Re-evaluate combined gates on TRENDING+VOLATILE+SIDEWAYS only (Fix 2 pattern — already in place for SIDEWAYS exclusion)
- Hypothesis: D2's TRENDING (1.195) + VOLATILE (1.452) + SIDEWAYS portion combined PF likely meets the 1.35 gate, and MDD drops below 10% once PRE_EXPIRY's 0.181 PF stops contributing

This avoids retraining on a restricted dataset (the E2 mistake) and instead filters at deployment time — exactly how C1 is currently deployed, just with a broader block list.

### Status

C1 (`staged_deep_hpo_c1_base_20260429_040848`) **remains the live model** with `regime_gate_v1` active. E2 will not be deployed. Stage-1-only ablation is the proposed next action — pending operator approval.

---

## 6. Launch Commands

```bash
gcloud compute ssh option-trading-ml-01 --zone=asia-south1-b
cd /opt/option_trading && git pull --ff-only

# Verify Fix 1 in pipeline.py
grep -n "ctx_regime_" ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py | head -5

tmux new -s e2
python3 -m ml_pipeline_2.run_research \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_e2_volatile_only.json
```

**Check status:**
```bash
RUN=$(ls ml_pipeline_2/artifacts/research/ | grep e2_volatile | tail -1)
cat ml_pipeline_2/artifacts/research/$RUN/summary.json | python3 -m json.tool | \
  grep -E '"status"|"blocking_reasons"|"block_rate"|"profit_factor"|"max_drawdown"'
```
