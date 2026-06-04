# Direction Model v2 — Published Model (Runtime Usage)

**Status:** published, gate-passed, **soft-overlay only, not yet live-cut-over** · **Date:** 2026-06-05
**Trained on:** ML VM `option-trading-ml-01` (amit-trading, asia-south1-b)
**Pairs with:** [ENTRY_MODEL_V2_PUBLISHED.md](ENTRY_MODEL_V2_PUBLISHED.md) · **History:** [STAGE2_DIRECTION_RECOVERY_PLAN.md](STAGE2_DIRECTION_RECOVERY_PLAN.md)

The entry model says *when* to trade; this says *which side* (CE vs PE). Direction is the system's hardest target (near-random), so this model is deployed as a **soft tilt / confidence filter — never a hard gate.**

---

## 1. TL;DR

- **Best direction result the project has produced: holdout AUC 0.593, ECE 0.013, drift 0.0095** — and *stable* (prior 6+ runs were ~0.52–0.55 with drift 0.13–0.19, the "regime amplifier" failure).
- It's still a **thin edge.** Value comes from **confidence gating**: on the ~20% of bars where it's most confident (prob ≥ 0.60) it calls direction right **64%** of the time vs 50% baseline.
- **Deploy as a soft overlay** (`direction_ml_policy.py`): blend weight 0.40, or an opt-in filter at min_prob 0.60. As a hard gate at full coverage (57%) it would bleed — don't.
- Positive class = **P(CE / up)**.

---

## 2. Artifacts

```
gs://amit-trading-option-trading-models/published_models/direction_only_v2/
├── direction_only_model_3m.joblib   # ACTIVE (+ _report.json)
├── direction_only_model_5m.joblib   # horizon-matched compare (+ _report.json)
└── active/direction_only_model.joblib   # == 3m
```
ML VM: `ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib` (active = 3m) and `…/published_v2/`.

---

## 3. Runtime usage

Consumer: `strategy_app/ml/direction_ml_policy.py` (wraps the entry policy when `DIRECTION_ML_MODEL_PATH` is set).

| Env var | Meaning | Recommended |
|---|---|---|
| `DIRECTION_ML_MODEL_PATH` | path to bundle | `/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib` |
| `DIRECTION_ML_WEIGHT` | blend weight of ML direction score (0=ignore, 1=ML-only) | **0.40** |
| `DIRECTION_ML_FILTER_MIN_PROB` | optional: block entries where direction conviction < this | **off** until shadow-validated, then `0.60` |

Two modes (both already implemented):
- **Conflict resolver (default):** when CE and PE both vote, pick the side with higher ML prob; blend with base score (so quality gates still hold).
- **Filter (opt-in):** require `P(side) ≥ DIRECTION_ML_FILTER_MIN_PROB`, else block. Use only after shadow.

Inference: builds the feature row from `project_stage_views_v2`, fills NaN with `feature_medians`, `prob = model.predict_proba[:,1]` (calibrated) = P(CE/up). Bundle kind must be `direction_only_bundle`.

---

## 4. The model

- **Label:** futures up-move wins over the next **3 minutes** (`direction_up`; 1=up=CE). 3m beat 5m (0.562) and 10m (0.529) — direction decays fast.
- **Features:** 114, empirically selected from `stage2_direction_view_v2` (NOT the old `fo_direction_entry_context_v1` regex, which silently dropped the `fut_return_*` momentum columns — the strongest signal). `oracle_rolling_*` dropped (no signal + live-inference gap).
- **Model:** LightGBM (depth 4, 300 trees, lr 0.03), isotonic-calibrated on the 2024-05→07 valid window. Holdout = OOS 2024-08→10 (23,995 bars, 50.0% up base rate).
- **Signal lives in:** short-horizon momentum (**reversion-signed** — recent up predicts next-3-min down), `vix_intraday_chg`, `pcr_change_5m/15m` (lead on tradeable bars), `ce_pe_oi_diff`, `dist_from_day_high/low`, ATM IV diff.

---

## 5. Performance & how to set the threshold

| min_prob | coverage | directional accuracy |
|---|---|---|
| 0.50 (all bars) | 100% | 56.9% |
| 0.55 | 43% | 61.2% |
| **0.60** | **20%** | **64.0%** |
| 0.65 | 2% | 65.1% |

Regime: stronger in up-trends (AUC 0.598) than down-trends (0.588) — both usable. Reliability is well-calibrated across bins (ECE 0.013).

**Operating guidance:** start as a soft tilt (`DIRECTION_ML_WEIGHT=0.40`, no filter) so it nudges side selection without blocking trades. After a clean shadow run, optionally add `DIRECTION_ML_FILTER_MIN_PROB=0.60` to skip low-conviction entries.

---

## 6. Live cutover (separate, deliberate — NOT done)

1. **Sim-validate** with the bundle wired (`DIRECTION_ML_MODEL_PATH` set, weight 0.40) over several days; confirm side-selection improves vs the `composite` heuristic and the prob histogram isn't collapsed.
   - ⚠️ Known live-inference gap: `ML_ENTRY_DIRECTION_MODE=consensus` historically never reached the strategy container (see memory `project_sim_live_3loss`). Verify the env actually propagates before trusting sim/live direction.
2. Sync `…/direction_only_v2/active/direction_only_model.joblib` to the runtime, set the env vars, restart strategy_app.
3. Confirm load log: `direction_ml_policy: loaded model … holdout_auc=0.593`.
4. Rollback: unset `DIRECTION_ML_MODEL_PATH` (reverts to base policy) and restart.

---

## 7. Caveats & how to improve further

- **Thin edge by nature.** 1-minute snapshot data structurally caps direction signal; the real unlock would be sub-minute/tick order-flow data.
- Next levers worth trying: regime-specialized models (it's already stronger in up-trends), a decisiveness-weighted objective, and 1m+3m horizon ensembling. Decisive-move filtering and magnitude-weighting were tested and **hurt** — don't revisit.
- **Not committed-then-deployed automatically;** artifacts only. **Not git-pinned to a release.**
- Direction remains the system bottleneck even after this — treat it as a tilt, lean on the entry gate + structural CE bias for the bulk of edge.

---

## Appendix — provenance
- Trainer/publisher: `ml_pipeline_2/scripts/train_publish_direction_v2.py` (LGBM + isotonic + reliability/confidence/regime tables).
- Diagnostic that picked the levers: `docs/DIRECTION_MODEL_V2.md` §4–5 (horizon sweep, feature ranking, lever tests).
- Reports: `direction_only_model_{3m,5m}_report.json` alongside each bundle.
