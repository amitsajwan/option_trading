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
| 2026-05-14 | `staged_deep_hpo_e2_volatile_only_<TS>` | — | — | — | — | — | — | 🔜 running |

(Update as E2 completes.)

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
