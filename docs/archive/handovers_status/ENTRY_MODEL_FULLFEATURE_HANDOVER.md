# Entry Model — Full-Feature Retrain · Handover for the ML Team

> **Date:** 2026-06-10 · **From:** strategy · **Goal:** train a **proper entry model that uses the FULL feature set** and is rigorously validated. Background ML work — runs on the ML VM while strategy continues live (paper).

> **TL;DR:** Recent retrains went *thin* — the 160pt HPO selected `fo_oi_pcr_momentum` (only **3 features**: `day_of_week, ce_pe_oi_diff, ce_pe_volume_diff`). The currently-deployed model (`entry_only_v3/020pct`, 51 features) is good but **v2-lineage, not a fresh validated train**. We need a **fresh, full-feature, well-validated** entry model. Lower-pts labels are acceptable (we want *more* entries) **if they pass the ship-gates**.

---

## 1. The problem to fix

- The entry HPO (`staged_dual_recipe.entry_s1_only_hpo_v2` and the 160pt/clean-move variants) lets the HPO **pick the best-scoring among a handful of CURATED feature subsets**. On the 0.30% label it picked a **3-feature OI/volume set** — too thin, fragile, doesn't use the rich snapshot (no IV-skew, walls, regime, velocity, structure).
- The deployed `entry_only_v3/020pct` (51 features, AUC ~0.83) is the full-feature one we run live, but its lineage is `entry_v2_5m_calibrated` — never cleanly re-trained/re-validated on the current pipeline. We can't fully trust or improve it without a proper retrain.

## 2. THE ASK — a full-feature entry model

1. **Feature set = COMPREHENSIVE.** Build/use a `fo_comprehensive` set = the **union of all engineered features**: velocity (`fo_velocity_v1`), OI/PCR momentum, IV-skew, **OI walls / max-pain distance**, regime/midday time-aware, asymmetry, market-structure, depth-if-available. **Do NOT let the HPO settle on a 3-feature subset** — either require the full set, or compare full-vs-subset explicitly and justify the choice on OOS economics, not in-sample AUC.
2. **Label = direction-agnostic magnitude, cost-clearing.** Sweep **X ∈ {50, 70, 110 pts}** (~0.10 / 0.13 / 0.20 %). Strategy currently wants **more entries → a lower-pts (50–70pt) model is acceptable IF it validates.** Pick the operating label by **validated trade-economics** (net after ~0.6%/leg cost, drop-outlier), not by AUC alone.
3. **HPO:** proper Optuna sweep across `xgb / lgbm / logreg` × the comprehensive feature set. Add **probability calibration** (isotonic/Platt) — the runtime gates on `ENTRY_ML_MIN_PROB`, so calibration matters.
4. **Entries-per-day check:** report, at the chosen threshold, **how many entries/day** on a normal vs trending vs choppy day. Strategy needs a model that actually fires a few times a day (the deployed v3/020 fired only ~2 on a quiet day).

## 3. Ship-gates (DO NOT skip — these caught every prior mirage)

1. **Separation** — fired bars clearly out-perform declined bars (not flat 0.5 everywhere).
2. **True OOS** — time-separated holdout quarters; report OOS, not in-sample.
3. **Drop-outlier robustness** — recompute net without the top trade and top 3; if it craters, it's noise.
4. **Calibration** — predicted prob ≈ realized hit frequency.
5. **Per-trade ≠ per-bar** — validate on actual trade P&L after cost, not per-bar accuracy.

## 4. How to run

- **VM:** `option-trading-ml-01` (project `amit-trading`, zone `asia-south1-b`). Venv at `/opt/option_trading/.venv` (has optuna — fixed in `88e36c3`).
- **Data:** `gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data` (2020→2024-10, full unfiltered chain), synced to `.data/ml_pipeline` on the VM by the template startup.
- **Runner:** `ops/gcp/run_ml_playground_overnight_vm.sh` (entry track) or `python -m ml_pipeline_2.scripts.run_entry_s1_only_hpo --config <manifest>`. **Branch:** `feat/intelligent-brain` (the pipeline + manifests live there, not on main).
- **Wrap to runtime bundle:** `ops/gcp/wrap_e6_into_entry_only_bundle.py` shows the `entry_only_bundle` shape the engine loads (`{kind, features, feature_medians, model}`). Build the bundle with the model's **real feature contract** (`_model_input_contract.required_features`) — newer pipeline runs store features there, not in the old `feature_columns`.

## 5. Definition of done

A **freshly-trained, full-feature** `entry_only_bundle` that, on **true OOS**:
- uses the comprehensive feature set (not a 3-feature subset),
- **beats `entry_only_v3/020pct`** on separation + validated trade-economics (drop-outlier-safe),
- is **calibrated**, and **fires a usable number of entries/day** at its operating threshold.

Strategy drops it in via `ENTRY_ML_MODEL_PATH` (no engine change), paper-validates (net after cost + drop-outlier), then live (1 lot). **Real money stays OFF until that passes.**

## 5b. Implementation wired up (2026-06-10, strategy)

The full-feature track is now coded and ready to run on the ML VM:

- **`fo_comprehensive` feature set** (`ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py`) — deliberate union of every engineered group (price/EMA/osc/VWAP-dist, futures+options flow, OI/PCR, IV structure, VIX + daily/intraday regime, velocity, oracle-rolling, time). Level-invariant: raw `px_*`, `vwap_fut`, absolute ATM strike and chain-row count are excluded. Resolves to **134/145** schema columns (verified). HPO **cannot** collapse to the 3-feature subset because this is the only stage-1 set in the new manifests.
- **Label-sweep manifests** (`ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_comprehensive_5m_{010,013,020}pct.json`) — `min_pct` ∈ {0.0010, 0.0013, 0.0020} (~50/70/110pt), no session filter, threshold grid down to 0.40, 6 models × 30 Optuna trials.
- **`publish_entry_calibrated.py`** — generalized, feature-set/label agnostic publisher. Reads the real feature contract (`feature_columns` → `_model_input_contract.required_features` → `feature_contract.json`), isotonic-calibrates on the held-out valid window, and emits all five ship-gates **plus drop-outlier robustness and entries/day** at the chosen threshold.
- **`ops/gcp/run_entry_fullfeature_retrain_vm.sh`** — detached orchestrator: runs all three HPOs, publishes + ship-gates each, writes calibrated bundles to `ml_pipeline_2/artifacts/entry_only/published_comprehensive/`. Never auto-installs active.

Run it on the ML VM (off-market): `sudo bash ops/gcp/run_entry_fullfeature_retrain_vm.sh start` then `... status`.

## 6. Context (so you don't repeat dead ends)

- Clean-move ("first-3-bars-same-direction") label is **REFUTED** — it smuggles in direction (unlearnable), AUC 0.49. See `docs/CLEAN_MOVE_RETRAIN_RESULT_2026-06-10.md`. Use a plain **magnitude** label.
- Entry quality is **not** the P&L bottleneck — **direction is** (see `docs/ENTRY_VS_DIRECTION_2026-06-08.md` + the 2026-06-10 direction-lever finding: big-move + momentum/OI/max_pain confluence ≈ 61% OOS). This entry retrain is a *refinement*; the direction model is the separate, higher-value track.
