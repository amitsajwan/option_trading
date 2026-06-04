# Entry Model v2 — Training Spec (for the modelling team)

**Status:** proposed — ready to implement
**Author:** trading + ML (Claude)  ·  **Date:** 2026-06-04
**Owner of next step:** modelling team
**Goal:** train a *selective, calibrated* Stage-1 entry-timing model on a **5-minute** horizon that actually discriminates good entry bars from bad — replacing the deployed model, which fires on ~100% of bars at its operating threshold.

---

## 0. Data availability (checked 2026-06-04)

**Yes — we have the training data, in full.** The flattened ML feature view used by the deployed model is on the **dev/build machine** at `.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/` (one parquet per trading day):

| Year | Trading days |
|------|--------------|
| 2020 | 251 |
| 2021 | 247 |
| 2022 | 247 |
| 2023 | 245 |
| 2024 | 209 (through **2024-10-31**) |
| **Total** | **1,199 days (2020-01-01 → 2024-10-31), ~4.75 yrs** |

Raw `futures/`, `options/`, `market_base/` parquet (back to 2020) are alongside it, so the feature view can be rebuilt if needed. This is a **single-stage / entry-only** model (`bypass_stage2=true, bypass_stage3=true, entry_only_publish=true`) — no staged dependencies.

**Where it is NOT:** the live VM's `.data/ml_pipeline/parquet_data/` is **empty** — training does **not** run on the VM. Run it on the dev/build box (or wherever `ml_pipeline_2` runs) that holds this parquet. Mongo on the VM holds only `phase1_market_snapshots_historical` = 2024-01-01→2024-10-31 (209 days) + small live 2026 — that's for replay/sim, not the primary training source.

**Gap to know:** data ends **2024-10-31**. There is no parquet for Nov-2024 → 2026. Live 2026 snapshots (collected since ~late-May-2026) sit in Mongo/JSONL and are **not yet flattened to parquet**. Implications:
- The spec's train/valid/holdout windows (2022→2024-10) are **fully covered** — train today, no data gathering needed.
- More regime diversity is available for free: **2020–2021** (COVID crash + bull) can be added to the training window. Their lower index levels (~30–38k vs ~54k now) reinforce the **%-based label** in §2.1.
- **Forward-validation on 2026** requires either the sim path (already wired: ops-sim + `analyze_sim_trace.py`) or running the mongo→parquet flattening ETL on recent live snapshots.

---

## 1. Why a new model (the problem in one paragraph)

The deployed entry model (`entry_s1_e6_soft50pts_10m`, label = "BankNifty futures move ≥50 pts in **either** direction within **10 min**") has a strong holdout **ROC-AUC 0.83** but is **useless as a gate in production**: on the 2026-06-04 replay it scored **min 0.669 / median 0.849 over 119 bars and fired on 100% of them** at the recommended `min_prob=0.65`. A 50-pt move in 10 min is nearly always achievable (median *realised* excursion that day was 66.8 pts), so the model learned to (correctly) say "yes" almost always. It cannot separate good entries from bad — all real filtering is done downstream by the gates, not the model. (Verified with `ops/gcp/analyze_sim_trace.py` → `verify_entry_label`: FIRED 119, moved 87 → "precision" 0.73 but **0 not-fired bars → separation unmeasurable**.)

The opposite extreme also failed: the **harsh** label (≥100 pts in 5 min, runs `entry_s1_only_hpo_v1/v2/v3`) had **~7% positive rate** and the probabilities **collapsed (max prob 0.276)** — the model could never clear a usable threshold. E6 fixed the collapse by softening to 50/10, but overshot into "always-on".

**The target is the middle:** a label hard enough that "fire" is selective (positive rate ~30–45%, probabilities span ~0.2–0.9), trained + **HPO-tuned + calibrated**, with a **data-driven operating threshold** and a **separation ship-gate**.

Key history (do not relearn these): see memory `project_e6_breakthrough_2026-05-23`, `project_entry_ml_dead_end`, and the E1–E6 ablation configs in `ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_*`.

---

## 2. Label spec (the 5-minute decision)

Oracle: `ml_pipeline_2/src/ml_pipeline_2/staged/entry_move_oracle.py` — label = 1 if `max(up_excursion, down_excursion) ≥ threshold` over the next `horizon_minutes`, where `threshold_pct = min_points / entry_price`.

### 2.1 Use a PERCENTAGE threshold, not fixed points
The training window spans BankNifty ~38,000 (2022) → ~52,000 (2024) → ~54,000 (2026). A fixed `min_points` is a **moving target in % terms** (50 pts = 0.13% in 2022 but 0.093% in 2026), which biases the label across the window. **Define the move in % of price.** The oracle already converts points→% per-bar, so either (a) pick `min_points` knowing it drifts, or — preferred — (b) **extend the oracle to accept `min_pct` directly** and pass that. *Small code change; recommended.*

### 2.2 Recommended threshold — MEASURED from 2022–2024 data (not estimated)
Horizon is **fixed at 5 minutes** (10-min is rejected — too slow for our exits). The 5-min forward max-excursion distribution was computed directly from the futures parquet (**703 trading days, 262,661 bars, 2022-01→2024-10**, median index 43,739):

| Threshold | Positive rate (5-min) | ≈ pts @ 2022–24 median (43.7k) | ≈ pts @ today (54k) |
|-----------|----------------------|-------------------------------|---------------------|
| 0.06% | 69.1% | 26 | 32 |
| 0.08% | 52.5% | 35 | 43 |
| **0.10%** | **39.5%** | **44** | **54** |
| 0.12% | 29.8% | 52 | 65 |
| 0.14% | ~24% | 61 | 76 |
| (fixed 100 pts) | 6.6% | — | — | ← the collapse regime (old harsh label) |

Median 5-min excursion = **36 pts = 0.083%**. Note 100 pts/5min = **6.6%** positive — matches the documented collapse (~7%, max prob 0.276).

**→ Primary label: 5 min, `min_pct = 0.10%` → 39.5% positive rate** (≈ 44 pts in the 2022–24 window, ≈ 54 pts at today's ~54k). This is the 5-minute analogue of the working base-rate of the old 10min/50pts label, at the shorter horizon — balanced and learnable, safely clear of the 7% collapse cliff.

**Use % not fixed points** (§2.1): a "50 pt" label is 0.13% in 2022 (≈27% positive) but 0.093% at 54k (≈45% positive) — inconsistent difficulty across the window. % keeps it constant.

**Sweep `min_pct ∈ {0.08, 0.10, 0.12, 0.14}%`** and pick by the §6 gates (target 30–40% positive + best separation). Expect **0.10–0.12%** to win.

> **Why "either direction" stays:** Stage-1 is a *timing/volatility* gate; the side is chosen by the direction model. Keep the symmetric move label. (If we later want an economically-aware entry label — "a long ATM option opened now reaches +X% before theta" — that is a separate Stage-1b experiment, noted in §8.)

---

## 3. Features

Use the proven set **`fo_velocity_v1`** (51 features, view `stage1_entry_view_v2`) — defined in `ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py`. It is the result of the E1→E6 ablations (velocity + OI/PCR momentum + IV structure + regime flags + time/expiry context + proven EMA/RSI/ATR/VWAP anchors). Do **not** expand blindly — `project_v3_microstructure_verdict` showed more microstructure features did not unlock edge.

### 3.1 Known feature caveat to FIX (production parity)
The `vel_*` velocity features are populated by `LiveVelocityAccumulator` only **from 11:30 IST onwards**. Morning bars (09:15–11:30) carry NaN velocity → imputed to `feature_medians` → the model runs on **degraded features in the morning**, exactly when many entries fire. Two options for the team:
1. **Segment-aware:** train/evaluate morning vs post-11:30 separately and report AUC per segment; or
2. **Backfill:** compute velocity from the rolling state in the morning too (preferred — closes the train/serve gap).
Flag whichever you pick in the run report.

---

## 4. Model + HPO

**This is the biggest missed opportunity: HPO was never run on a balanced label.** E6 (the winning label) was a **fixed** config (`hpo.enabled=false`); the only HPO runs (`entry_s1_only_hpo_v1/v2/v3`) were on the *harsh* 100/5 label that collapsed. So no tuned model exists for a good label.

- **Base model:** `xgb_balanced` (`max_depth 4, n_estimators 350, lr 0.03, subsample 0.85, colsample 0.85, reg_lambda 2.0`) — `ml_pipeline_2/src/ml_pipeline_2/catalog/models.py`.
- **Candidates to search:** `xgb_balanced, xgb_regularized, xgb_shallow, lgbm_fast, lgbm_dart, logreg_balanced` (logreg as a linear sanity baseline).
- **HPO:** Optuna, **enabled**, `trials_per_model: 24`, `max_experiments: 48`, objective **brier** (calibration-aware; AUC reported alongside). Search space already implemented in `ml_pipeline_2/src/ml_pipeline_2/model_search/search.py` (XGB: depth ±2, n_estimators 0.6–1.8×, lr log 0.008–0.12, subsample/colsample, reg_alpha/reg_lambda; LGBM adds num_leaves, min_child_samples).
- **Class handling:** with a ~35–40% positive rate, prefer `scale_pos_weight≈1`; do **not** over-balance — over-weighting positives is part of what pushed E6 probabilities high.

---

## 5. Calibration (NEW — currently absent)

The deployed bundle has **no calibrator** (`calibrated=None`), and its production probabilities (0.67–0.98) do not match its training calibration (0.2–0.8) → this is a core reason it "fires always". **Add explicit calibration:**
- Fit **isotonic** (or Platt if data-thin) on a held-out calibration slice *inside* the walk-forward, never on train.
- **Ship gate:** reliability curve on the OOS holdout must be monotonic with ECE ≤ 0.05. Report the reliability table in the run report.
- Re-derive the operating threshold (§7) **after** calibration.

---

## 6. Validation protocol & SHIP GATES

Windows (keep existing): train `2022-01-01 … 2024-04-30`, valid `2024-05 … 07`, **OOS holdout `2024-08 … 10`**. Walk-forward CV `train 84 / valid 21 / test 21 / step 21` (purged). Then **forward-validate on live-collected 2026 data** via the ops-sim + `analyze_sim_trace.py`.

A model **ships only if all hold** (tighten the existing `hard_gates`):
1. **Label balance:** positive rate ∈ [0.30, 0.45] on train and holdout.
2. **Discrimination:** OOS holdout **ROC-AUC ≥ 0.62** (was 0.55; we want real signal, not "always yes").
3. **Stability:** `roc_auc_drift_half_split ≤ 0.08`.
4. **Calibration:** ECE ≤ 0.05, monotonic reliability curve (§5).
5. **Separation (the key new gate):** at the chosen operating threshold, run `verify_entry_label` (`strategy_app/sim/trace_digest.py`) on the OOS sim — **precision(fired) − base-rate(not-fired) ≥ +0.10**. This is the test the current model fails (it never produces not-fired bars). A model that can't produce a meaningful not-fired set is rejected.
6. **Probability spread:** fired-bar prob distribution must not collapse to one bucket; ≥ 3 of the 5 histogram buckets populated.

---

## 7. Operating threshold (stop hardcoding 0.65)

Do **not** ship `min_prob=0.65` by default. Choose the threshold on the **OOS holdout** as the point that yields the target **selectivity** (e.g., fire on the top ~30% of bars by calibrated prob) AND maximises separation (§6.5). Report the precision/recall/selectivity curve. Expose per-regime thresholds if separation differs by regime (the data on 2026-06-04 suggests SIDEWAYS bars are where the model over-fires).

---

## 8. Deliverables & how to run

1. New config `ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_v2_5m_sweep.json` — clone `entry_s1_e6_soft50pts_10m.json`, set `horizon_minutes: 5`, `min_pct` (sweep 0.06–0.12%), `hpo.enabled: true` (24 trials), candidate model list from §4, add isotonic calibration, tightened `hard_gates` from §6.
2. Run the staged pipeline (same entrypoint as prior runs; `bypass_stage2/3: true`, `entry_only_publish: true`).
3. Publish bundle to `ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib` (keep `.bak` of the current one).
4. Validate with the sim: deploy branch, run ops-sim for several days, read the auto-generated digest (`analysis.markdown` on the job / `/tmp/sim_<job>/trace_report.md`) and confirm the §6 gates — especially **separation > 0** and a non-collapsed prob histogram.
5. Record in the run report: label base rate, AUC, drift, ECE/reliability, chosen threshold, separation, per-regime breakdown, morning-feature handling (§3.1).

---

## 9. Open decisions for the team
- **§2.1** add `min_pct` to the oracle (recommended) vs keep `min_points` and accept level drift?
- **§3.1** morning velocity: backfill (close train/serve gap) vs segment-aware reporting?
- **§8** keep the symmetric movement label, or also prototype an economically-aware "long-option-profitable" Stage-1b label (separate experiment)?
- Direction model is a **separate, known-weak** component (`direction_only_model`); this spec does not address it. Note that live currently runs `composite` heuristic direction (the `ML_ENTRY_DIRECTION_MODE=consensus` env never reaches the strategy container — see memory `project_sim_live_3loss_2026-06-04`), so even a perfect entry model is bottlenecked by direction. Sequence the direction-model work right after this.

---

## Appendix — quick reference
- Label oracle: `ml_pipeline_2/src/ml_pipeline_2/staged/entry_move_oracle.py`
- Feature set: `fo_velocity_v1` in `ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py` (view `stage1_entry_view_v2`)
- Models + HPO: `ml_pipeline_2/src/ml_pipeline_2/catalog/models.py`, `.../model_search/search.py`
- Reference configs: `staged_dual_recipe.entry_s1_e6_soft50pts_10m.json` (deployed, fixed), `entry_s1_only_hpo_v1.json` (HPO template, harsh label)
- Verification tooling: `strategy_app/sim/trace_digest.py` (`verify_entry_label`), `ops/gcp/analyze_sim_trace.py` (CLI)
- Deployed model card: AUC 0.830, Brier 0.170, drift 0.020, 51 features, label 50pts/10min, no calibrator.
