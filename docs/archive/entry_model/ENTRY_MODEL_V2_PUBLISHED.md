# Entry Model v2 — Published Models (Runtime Usage)

**Status:** published, gate-passed, **not yet live-cut-over** · **Date:** 2026-06-04
**Trained on:** ML VM `option-trading-ml-01` (amit-trading, asia-south1-b)
**Spec:** [ENTRY_MODEL_V2_SPEC.md](ENTRY_MODEL_V2_SPEC.md) · **Replaces:** deployed E6 (`entry_s1_e6_soft50pts_10m`, fired on ~100% of bars)

This doc is everything a runtime/ops team needs to deploy and operate the v2 entry models. It does **not** require reading the training code.

---

## 1. TL;DR

- A **5-minute, level-invariant, isotonic-calibrated** Stage-1 entry-timing model. It answers one question per bar: *"is now a good time to enter?"* — it does **not** pick side (CE/PE); the direction model does that.
- **Four models pass all ship-gates** — a selectivity ladder by label tightness (0.08 / 0.10 / 0.12 / 0.14% move). The **active winner is `010pct`** (0.10%, the primary tradeable target). The others are published alternates: `008pct` = more coverage, `012pct`/`014pct` = ultra-selective / high-conviction-only.
- Unlike E6 (AUC 0.83 but fired always), v2 is **selective and calibrated**: at its operating threshold it fires on ~22% of bars with ~66% precision vs ~19% base rate among declined bars.
- Drop-in replacement: same bundle format (`kind=entry_only_bundle`), same runtime loader. **No code change needed** to deploy.

---

## 2. Where the artifacts are

**GCS (durable, source of truth for deploy):**
```
gs://amit-trading-option-trading-models/published_models/entry_only_v2/
├── entry_only_model_010pct.joblib          # winner       (+ _report.json)
├── entry_only_model_008pct.joblib          # alternate     (+ _report.json)
├── entry_only_model_012pct.joblib          # selective     (+ _report.json)
├── entry_only_model_014pct.joblib          # ultra-selective(+ _report.json)
├── README_RUNTIME.md                       # this doc
└── active/
    ├── entry_only_model.joblib             # == 010pct (the one to deploy)
    └── entry_only_report.json
```

**On the ML VM** (`/opt/option_trading/`):
```
ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib   # active = 010pct
ml_pipeline_2/artifacts/entry_only/published_v2/                       # both bundles + reports
ml_pipeline_2/artifacts/v2_sweep/<tag>/                                # raw research runs (summary.json etc.)
```

Each bundle has a sibling `*_report.json` with the full metric tables (reliability, separation, gates) — same content as §5–6 below.

---

## 3. How the runtime consumes it

Loader: `strategy_app/ml/bundle_inference.py` · Strategy: `strategy_app/engines/strategies/ml_entry.py`

Two env vars control it:

| Env var | Meaning | Set to |
|---|---|---|
| `ENTRY_ML_MODEL_PATH` | path to the bundle inside the container | `/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib` |
| `ENTRY_ML_MIN_PROB` | fire threshold; entry fires when `prob ≥ min_prob` | **`0.50`** for the active 010pct model (see §6) |

Inference flow (per bar, already implemented — nothing to change):
1. `load_joblib_bundle(path, expected_kind="entry_only_bundle")`.
2. Build the 51-feature row from the live snapshot (`project_stage_views_v2` + velocity).
3. Fill any NaN feature with `bundle["feature_medians"][feature]`.
4. `prob = bundle["model"].predict_proba(frame)[0, 1]` — **already calibrated** (isotonic baked into the model object).
5. Fire ENTRY vote if `prob ≥ ENTRY_ML_MIN_PROB`.

The `model` object is a scikit-learn `CalibratedClassifierCV` wrapping the XGBoost pipeline, so `predict_proba` returns calibrated probabilities directly — no extra calibration step in the runtime.

**Bundle schema (`kind=entry_only_bundle`):** `features` (51 names), `feature_medians` (dict), `model` (calibrated, has `predict_proba`), `recommended_min_prob`, `operating_point`, `holdout_eval`, `reliability_table`, `separation_table`, `ship_gates`, `training_metadata`.

---

## 4. The label (what the model predicts)

- **Target:** 1 if BankNifty **futures** move ≥ `min_pct` of price in **either** direction within the next **5 minutes**; else 0.
- **Level-invariant `min_pct`** (price fraction), *not* fixed points — so the label means the same thing at 38k (2022) and 54k (today). This is the key fix vs a fixed-point label, which drifts looser as the index rises.
- **Direction-agnostic by design** — Stage-1 is a timing/volatility gate. Side is the direction model's job.
- Features: `fo_velocity_v1` (51) on view `stage1_entry_view_v2` — velocity + OI/PCR momentum + IV structure + regime/time/expiry context + EMA/RSI/ATR/VWAP.

---

## 5. Model details

Both: model = `xgb_shallow` (depth 2, 500 trees, lr 0.025, subsample/colsample 0.9, reg_lambda 2.0), 51 features, isotonic-calibrated on the 2024-05→07 valid window. Holdout = OOS **2024-08→10** (23,995 bars). Trained on 2022-01→2024-04.

### 5.1 `010pct` — ACTIVE WINNER (label = 0.10% move)
| Metric | Value | Gate | Pass |
|---|---|---|---|
| Holdout ROC-AUC | **0.820** | ≥0.62 | ✅ |
| Holdout Brier | **0.150** | ≤0.24 | ✅ |
| ECE (raw → calibrated) | 0.032 → **0.018** | ≤0.05 | ✅ |
| AUC drift (half-split) | <0.02 | ≤0.08 | ✅ |
| Separation @ op-thr | **+0.472** | ≥0.10 | ✅ |
| Prob spread | 0.00 → 1.00 | not collapsed | ✅ |
| Holdout base rate | 28.9% | — | — |

Reliability (calibrated, holdout) — confidence ≈ accuracy across all bins (well-calibrated):
`0.0-0.1: conf .052/acc .036 · 0.2-0.3: .234/.248 · 0.4-0.5: .445/.443 · 0.6-0.7: .665/.659 · 0.9-1.0: .941/.908`

### 5.2 Published alternates — full sweep (all pass every gate)
All four are the same `xgb_shallow` + isotonic-calibration recipe; they differ only in label tightness. Tighter labels have lower base rate → lower Brier and fire less often, but on a rarer (less tradeable) target.

| Tag | Label move | Holdout AUC | Brier | ECE (cal) | Base rate | Op thr | Fire% | Precision(fired) | Separation |
|---|---|---|---|---|---|---|---|---|---|
| `008pct` | 0.08% | 0.823 | 0.168 | 0.018 | 41.3% | 0.70 | 19.8% | 0.819 | +0.506 |
| **`010pct` ★** | **0.10%** | **0.820** | **0.150** | **0.018** | 28.9% | **0.50** | 22.0% | 0.657 | +0.472 |
| `012pct` | 0.12% | 0.820 | 0.124 | 0.015 | 20.3% | 0.45 | 11.6% | 0.623 | +0.474 |
| `014pct` | 0.14% | 0.823 | 0.101 | 0.011 | 14.9% | 0.45 | 7.3% | 0.578 | +0.463 |

**Why `010pct` is the active pick:** it targets the most tradeable move size (0.10% ≈ a real 5-min swing), keeps the best label balance on the training window (40.6%), and fires at a usable rate (~22%) with solid precision. `008pct` trades more often (looser target); `012/014pct` are high-conviction-only (fire 7–12%) — useful if you want very few, very confident entries, but their precision is on a rarer target.

---

## 6. Operating threshold (`ENTRY_ML_MIN_PROB`)

Threshold is **selectivity**, chosen on holdout. Pick per how often you want to fire. The full holdout curves:

**010pct (ACTIVE) — recommended `ENTRY_ML_MIN_PROB=0.50`:**
| min_prob | fire % | precision(fired) | base(not-fired) | separation |
|---|---|---|---|---|
| 0.45 | 27.1% | 0.622 | 0.166 | +0.457 |
| **0.50** | **22.0%** | **0.657** | 0.186 | **+0.472** |
| 0.55 | 14.9% | 0.717 | 0.214 | +0.503 |
| 0.60 | 10.7% | 0.768 | 0.232 | +0.536 |
| 0.70 | 7.2% | 0.820 | 0.248 | +0.572 |

Raise toward 0.55–0.60 for fewer, higher-precision entries; lower toward 0.45 for more coverage.

**Recommended `ENTRY_ML_MIN_PROB` per model** (each model's probs sit on a different scale, so the threshold is model-specific):

| Model | Recommended `ENTRY_ML_MIN_PROB` | Fire% | Precision |
|---|---|---|---|
| `008pct` | 0.70 | 19.8% | 0.819 |
| **`010pct` (active)** | **0.50** | 22.0% | 0.657 |
| `012pct` | 0.45 | 11.6% | 0.623 |
| `014pct` | 0.45 | 7.3% | 0.578 |

> The models are **not** interchangeable at the same threshold. If you switch the active model, switch `ENTRY_ML_MIN_PROB` to that model's value (it's also stored in each bundle's `recommended_min_prob`).

---

## 7. Deploying to live (separate, deliberate step — NOT done yet)

Publishing wrote artifacts only; **live trading is unchanged.** To cut over:

1. **Sim-validate first** (spec §8.5): run ops-sim for several days with the v2 bundle, read `analyze_sim_trace.py` digest, confirm separation ≥ +0.10 and a non-collapsed prob histogram on live-like 2026 data.
2. Make the bundle available to the runtime container (sync `gs://…/entry_only_v2/active/entry_only_model.joblib` to the runtime VM, or bake into the image at `/app/ml_pipeline_2/artifacts/entry_only/published/`).
3. Set runtime env: `ENTRY_ML_MODEL_PATH=/app/.../entry_only_model.joblib` and `ENTRY_ML_MIN_PROB=0.50`.
4. Restart the strategy container; confirm the load log line: `ml_entry: loaded entry model … holdout_auc=0.82 min_prob=0.50`.
5. **Rollback:** the previous E6 bundle is preserved; repoint `ENTRY_ML_MODEL_PATH` (or restore the `.bak`) and restart.

---

## 8. Caveats & status

- **Not committed to git.** Code changes (oracle `min_pct`, `publish_entry_v2_calibrated.py`, 4 sweep configs) live on branch `feat/decision-trace-structure-sim-analyzer` working tree + the ML VM. Commit before relying on reproducibility.
- **Holdout volatility compression:** 2024-08→10 had ~10–12 pts lower 5-min excursion than the training window, so the 010pct holdout positive rate (28.9%) sits just under the spec's 0.30 label-balance floor. Not a model defect — discrimination/calibration/separation all pass strongly.
- **Morning velocity (spec §3.1):** `vel_*` features are NaN before 11:30 IST and filled with `feature_medians`. Medians are computed from a real recent slice (2024-02→07), so this is sane, but the train/serve gap is not fully closed (backfill vs segment-aware is an open decision).
- **Direction is the next bottleneck:** live runs `composite` heuristic direction; even a perfect entry gate is capped by direction quality. Sequence direction-model work next.
- **`min_pct` units:** price fraction. 0.0010 = 0.10%, 0.0008 = 0.08%.
- **sklearn note:** calibrator uses `CalibratedClassifierCV(cv="prefit")` (deprecated in sklearn ≥1.6, removed 1.8). Pin sklearn <1.8 in the runtime, or re-export with `FrozenEstimator` when upgrading.

---

## Appendix — provenance
- Research runs: `ml_pipeline_2/artifacts/v2_sweep/{008pct,010pct}/summary.json` (decision PUBLISH, all gates pass).
- Publisher: `ml_pipeline_2/scripts/publish_entry_v2_calibrated.py` (adds isotonic calibration + reliability/separation tables + real medians; the older `export_entry_bundle_from_research.py` skips calibration and zeroes medians — do not use for v2).
- Configs: `ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_v2_5m_{008,010,012,014}pct.json`.
