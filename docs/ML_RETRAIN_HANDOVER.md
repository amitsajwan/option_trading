# ML Entry-Model Retrain — Handover for the ML Team

> **Date:** 2026-06-09 · **From:** strategy · **Goal:** retrain the **ENTRY model only** with a **clean-move label** (direction-agnostic). Direction is OUT of scope here — separate, later.
>
> Read this top to bottom — the *label* section is the actual ask; everything else is context + how-to.

> **TL;DR (one line for the team):** Retrain the **entry model** with the label **`|move| ≥ X% within Y min AND the first 3 bars are in the same direction`** — a single binary, direction-agnostic. The "first 3 bars same direction" forces a *clean start* so the model stops firing on chop. Ship only if it beats the current entry model on **separation + OOS** and is **calibrated**.

---

## 1. Why we're retraining (the findings that matter)

After exhaustive testing on live data, here's what's established:

| Finding | Evidence |
|---|---|
| **Entry model works** — finds big moves | 82% of fired bars get a ≥70pt move |
| **Direction is a coin-flip** — the bottleneck | 53–59% side accuracy; refuted 3 independent ways |
| **The "direction model" is a *magnitude* detector** | its confidence predicts move *size* (avgW 2.6→4.3%) but never the *side* (≤50%) |
| **Current entry label counts CHOP as positive** | label = "moved ≥X pts" — up 70 / down 70 / net 0 still = YES |
| **No robust edge exists with current models** | best config's "+edge" was 1–2 outlier winners; drop them → −15 to −31% |

**Root cause:** the label is **magnitude-only and direction-agnostic.** It trains the model to fire on chop (where options bleed), and it never learns trend-vs-chop. **Fix the label → fix the model.**

---

## 2. THE ASK — the new entry label (this is the whole job)

Retrain the **entry model only** — a single, **direction-agnostic** binary classifier — with a **clean-move** label.

### Label definition
For a signal at bar **T**, over the next **Y minutes** (start with Y = 5 and Y = 10):

```
ENTRY_POSITIVE = |Close(T+Y) - Close(T)| >= X
                 AND
                 the first 3 bars after T are all in the same direction
                 (all up, or all down — i.e. the move starts clean, no reversal)
```

- **Direction-agnostic:** a clean UP move and a clean DOWN move are **both POSITIVE.** This is an entry/magnitude model — it predicts *"a clean move is coming,"* NOT which way. (Direction is a separate problem, handled later — do not build it here.)
- **X = the move that clears cost**, not a round number. Costs ≈ 0.6%/leg; set X so the *option* clears cost (≈0.10–0.20% underlying / ~70–110 pts; tune it). Express in **% not raw points** (level-invariant).
- **"first 3 bars same direction"** is the key addition vs the old label — it forces a **clean start** so the model stops firing on chop (up-50 / down-50 / net-0 ranges that the old `|move|≥X` label counted as positive).

### Variants to sweep (pick the most learnable)
| Variant | "clean start" condition | Note |
|---|---|---|
| **strict** | all 3 bars same direction | cleanest, fewest positives |
| **soft** | 2-of-3 same direction, OR net-positive over first 3 | more samples — **fallback if strict is too sparse** |
| **efficiency** | `\|net\| / path >= 0.6` over Y | continuous "cleanliness" |

### What this buys us
- The model learns **trend-vs-chop** from features — it stops firing on ranges where options bleed.
- It's a **drop-in replacement** for the current entry model (same direction-agnostic shape) — the engine already consumes an `entry_only_bundle` via `ENTRY_ML_MODEL_PATH`; no engine change needed.
- Fires **less often** (clean moves are rarer than any-move) — intended: *"big move, big profit, but less."*

> **Scope note:** this is ENTRY only. To *trade*, the system still needs a direction (CE/PE) — that stays on the existing mechanism for now and is a **separate future task.** Do not couple direction into this model.

---

## 3. How to run it (ML VM)

**Runbook:** [`docs/ML_PLAYGROUND_OVERNIGHT.md`](ML_PLAYGROUND_OVERNIGHT.md) — one-command overnight Optuna HPO for entry + direction, then feature-set grids.
**Script:** [`ops/gcp/run_ml_playground_overnight_vm.sh`](../ops/gcp/run_ml_playground_overnight_vm.sh) (+ `preflight_ml_playground.sh`, `summarize_ml_playground_overnight.sh`).
**Prior spec (for label/HPO/calibration mechanics):** [`docs/ENTRY_MODEL_V2_SPEC.md`](ENTRY_MODEL_V2_SPEC.md) — **but override its magnitude-only label with §2 above.**

**Models/features already wired** (see runbook): xgb / lgbm / logreg via Optuna; feature sets `fo_velocity_v1`, `fo_midday_*`, `fo_oi_pcr_momentum`. Add the new label heads to the recipe manifests.

**Step 1 is to add the new label** to the labeler, then run the playground. Confirm the VM name before launching (runbook says `option-trading-runtime-01`; a dedicated `option-trading-ml-01` may also exist — verify which has the GPU/data).

---

## 4. Data

- **Training data is on the DEV BOX:** `.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/` — **1199 days, 2020 → 2024-10**, full unfiltered option chain.
- **Gap:** no data Nov-2024 → 2026 (forward-collected live data is only ~10 days, on the runtime VM mongo — *not* enough to train/validate on; ignore it for training).
- The label needs the **forward bars** (T+1..T+Y) and **per-bar direction** — present in the parquet.

---

## 5. Ship-gates (DO NOT skip — these caught every mirage we hit)

A model/config is **only** validated if it passes ALL:

1. **Separation** — fired bars must clearly out-perform declined bars (not a flat 0.51 prob everywhere).
2. **True OOS** — hold out time-separated quarters; report OOS, not just in-sample.
3. **Drop-outlier robustness** — recompute net **without the single best trade** and **without the top 3**. If it craters, it's noise. *(This is the test that killed our "+1.16%" lottery — it was 2 lucky winners on an 18-trade bleed.)*
4. **Calibration** — predicted prob ≈ realized frequency (the current direction model is *anti*-calibrated for side).
5. **Per-trade ≠ per-bar** — validate on actual *trade* P&L after cost, not just per-bar accuracy.

---

## 6. Honest priors / cautions (so you don't repeat our dead ends)

- **Don't assume direction gets easy on big moves.** We *gated* the existing model to high-confidence (= big-move) bars and direction stayed ≤50%. A clean-directional *label* is a different mechanism and may do better — **but prove it, don't assume it.**
- **Costs are brutal** (~0.6%/leg vs ~2.5% typical move). The label must clear cost or nothing else matters.
- **Fewer trades is fine.** A model firing 2–4×/day on clean trends at 60%+ side accuracy beats 30×/day at coin-flip.
- If clean-directional direction *still* can't beat ~58–60%, the honest fallback is **structural**: sell-side (collect premium, inverts cost math) or a longer horizon. Flag it early rather than over-tuning.

---

## 7. Definition of done

A published **`entry_only_bundle`** that, on **true OOS**:
- **beats the current entry model** at predicting clean moves (separation + AUC on the clean-move label),
- is **calibrated** (predicted prob ≈ realized clean-move frequency),
- and fires on **higher-quality (cleaner) setups** — verifiable by lower chop-rate among fired bars.

Strategy then drops it into the live engine via `ENTRY_ML_MODEL_PATH` (no engine change), pairs it with the existing direction mechanism, and runs the **trade-level** validation (net after cost + drop-outlier robustness, §5.3) in **paper** before any live change. *(Direction remains the known open problem — a clean-move entry alone won't fix P&L if direction stays a coin-flip; that's a separate, later task.)*

**Questions / context:** see `docs/ENGINE_DECISION_FLOW.md` (how the live engine consumes the model) and the memory notes on the direction findings.
