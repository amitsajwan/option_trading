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

## 5. Verdict & decision

- **Single model**, selectivity via threshold (multiple = redundant; proven).
- **Compression features earn a small, real keep (+0.009)** and are wired into both live and
  historical paths (one shared module → no serve-skew). Worth keeping *if* the v2-view trained
  model is ≥ v3; otherwise v3 stays (the +0.009 doesn't justify 12-feature maintenance for a
  sub-v3 result).
- **Entry gate → ML-only**, wrapped in the Selection Gate (rank-vs-today + cost floor + budget),
  with **feature-freshness → abstain** as the only safety (drop the ATR-OR).
- **The honest overlay:** this round did entry *right* and proved move-detection is saturated.
  **None of it moves P&L** — profitability still hinges entirely on **direction + the ~108pt cost**.
  The next experiment (direction on the move-positive subset, follow vs **fade ~59%**) is the one
  that decides whether the system makes money.

## Reproduce
- Configs: `ml_pipeline_2/configs/research/staged_dual_recipe.bmm_*.json`,
  `…ab_5m020_*.json`, `…bmm_prod_5m020_v2view.json`.
- Feature module: `snapshot_app/core/compression_features.py` (shared live+historical).
- Watcher: `ml_pipeline_2/scripts/bmm_results.py`.
