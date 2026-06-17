# BMM (Big-Move Model) — Results & Verdict

_Compression/stored-energy feature refinement of the entry magnitude model, per the proposer
review ([BMM_PROPOSAL_REVIEW_BRIEF.md](BMM_PROPOSAL_REVIEW_BRIEF.md)). Branch
`feat/compression-state-engine`. Trained on ML VM, 2022–2024 walk-forward, holdout 2024-08→10.
Baseline to beat: live `entry_only_v3` = AUC **0.831**._

## 1. Multi-horizon grid (Lean-5, `fo_bmm_v1`, candidate view)

| model | move target | AUC | OOS drift (½1→½2) | win% @0.45 | prec @top-decile | block-rate | max-DD |
|---|---|---|---|---|---|---|---|
| h05m | ≥54pt / 5m | 0.791 | 0.005 (0.788→0.794) | 0.70 | 0.72 | 0.42–0.73 | 2.6% |
| h10m | ≥80pt / 10m | 0.780 | **0.001** | 0.70 | 0.71 | 0.50–0.77 | 2.6% |
| h15m | ≥108pt / 15m | 0.773 | — | — | — | — | — |
| h20m | ≥160pt / 20m | 0.764 | 0.044 | 0.70 | 0.74 | 0.85–0.95 | 1.4% |
| h30m | ≥216pt / 30m | 0.771 | 0.046 | 0.72 | 0.75–0.80 | 0.93–0.96 | 0.6–1.9% |

All pass the publish gates on paper (PF >3, MDD <3%, drift <0.08, block-rate healthy).
**Caveat:** PF/win/MDD here are move-detection P&L at *underlying scale* — NOT option-cost-aware,
NOT direction-dependent. They say the detector is good, not that the system is profitable.

## 2. The horizons select the SAME entries (key finding)

Scoring all four on the 24,059-bar holdout:

- **Probability correlation 0.92–0.99** across every pair (adjacent horizons 0.99).
- **Perfect nesting:** `P(short fires | long fires) = 1.00` for every pair — the selective
  long-horizon fires are a strict high-confidence **subset** of the broad short-horizon fires.
- Top-10% bars overlap 0.71–0.89.

**Implication:** horizon is a *sensitivity knob on one signal*, not a set of distinct strategies.
Multiple models buy **zero diversification** — they vote on the same bars. → **Ship ONE model;
control selectivity by threshold.** Move-detection is **saturated** (every variant agrees on
*when* a move is coming) — consistent with the long-standing finding that move-detection ≈ ATR
and was never the bottleneck.

## 3. Do the compression features actually help? (controlled A/B)

Same 5m/0.20% label, same candidate view, differ ONLY by feature set:

| run | feature set | AUC |
|---|---|---|
| `ab_5m020_base` | fo_velocity_v1 (baseline) | 0.806 |
| `ab_5m020_bmm` | fo_bmm_v1 (+compression) | 0.815 |

**Compression contribution = +0.009 AUC.** Real and positive (matches the E1 "modest but real"
finding), but small — it is roughly the ceiling of what feature work buys on a saturated detector.

## 4. The v3 gap was mostly the VIEW, not the features

- baseline on **candidate** view = 0.806; live **v3** on the **v2** view = 0.831 → the candidate
  view is a ~0.025-weaker backbone.
- So the earlier "BMM (0.79) < v3 (0.83)" was **mostly the research view**, not a real loss.
- **Production model** therefore trains `fo_bmm_v1` @ 5m/0.20% on the **v2 view** (= what live
  serving emits via `project_stage_views_v2`, so zero serve-skew). Projection: 0.806 + 0.025
  (view) + 0.009 (features) ≈ **0.84**, i.e. likely beats v3 — but this is **arithmetic, not yet
  trained**; the `bmm_prod_5m020_v2view` run settles it from a real number.

## 4b. PRODUCTION RUN (decisive) — compression does NOT beat v3

Trained `fo_bmm_v1` @ 5m/0.20% on the **v2 view** (v3's exact recipe, only the feature set
changed → a clean like-for-like feature A/B; both resolve to the identical 83 features incl.
all 12 compression cols):

| model | feature set | view | AUC |
|---|---|---|---|
| **v3 (live)** | fo_velocity_v1 | v2 | **0.831** |
| bmm_prod | fo_bmm_v1 (+compression) | v2 | **0.8146** (drift 0.056, brier 0.080) |

**On the serve-parity v2 view, the compression feature set is −0.016 BELOW v3.** And note the
sign flip vs §3:
- candidate view: fo_bmm_v1 (0.815) **>** fo_velocity_v1 (0.806) → +0.009
- v2 view: fo_bmm_v1 (0.8146) **<** fo_velocity_v1 (0.831) → −0.016

The "+0.009" did **not** generalize — it reversed on the production view. So the compression
features are **within view-dependent noise, not a robust improvement.** The projected ~0.84
(0.806 + view + features) was wrong: the v2-view benefit accrued to the *velocity* features
(0.806→0.831), not the compression set (candidate 0.815 ≈ v2 0.8146).

## 5. Verdict & decision

- **Single model**, selectivity via threshold (multiple = redundant; proven).
- **Compression features do NOT robustly beat v3.** On the clean serve-parity v2-view A/B,
  fo_bmm_v1 (0.8146) is −0.016 below v3 (0.831); the candidate-view +0.009 flipped sign → noise.
  **RECOMMENDATION: keep `entry_only_v3` as the entry model.** Shipping a sub-v3 model plus the
  12-feature live-parity maintenance burden is not justified by a non-robust, view-dependent
  delta. (The shared compression module stays in the codebase — cheap, already wired, and useful
  as candidate features for *direction* — but is not the production entry model.)
- **Entry gate → ML-only is still the right design** (v3 prob → Selection Gate → trade,
  freshness→abstain, drop ATR-OR). Just point it at **v3**, not the compression model.
- **The honest overlay (now reinforced):** every feature variant lands at ~0.81–0.83; move-
  detection is **saturated** and v3 is at the ceiling. **None of this moves P&L** — profitability
  hinges entirely on **direction + the ~108pt cost**. The next experiment (direction on the
  move-positive subset, follow vs **fade ~59%**) is the only thing that decides whether the
  system makes money. **That is where remaining effort should go.**

## Reproduce
- Configs: `ml_pipeline_2/configs/research/staged_dual_recipe.bmm_*.json`,
  `…ab_5m020_*.json`, `…bmm_prod_5m020_v2view.json`.
- Feature module: `snapshot_app/core/compression_features.py` (shared live+historical).
- Watcher: `ml_pipeline_2/scripts/bmm_results.py`.
