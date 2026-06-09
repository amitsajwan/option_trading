# ML Retrain — Handover for the ML Team

> **Date:** 2026-06-09 · **From:** strategy · **Goal:** retrain entry (and direction) with a **clean-directional label**, because the current magnitude-only label is the proven root cause of an unprofitable system.
>
> Read this top to bottom — the *label* section is the actual ask; everything else is context + how-to.

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

## 2. THE ASK — the new label (this is the whole job)

Train entry as **two directional heads** on a **clean-directional** label, in **option-return / cost-aware** space.

### Label definition
For a signal at bar **T**, over the next **Y minutes** (start with Y = 5 and Y = 10):

```
CLEAN_UP   = (Close(T+Y) - Close(T)) >= X    AND   first N bars after T are up
CLEAN_DOWN = (Close(T) - Close(T+Y)) >= X    AND   first N bars after T are down
```

- **X = the move that clears cost**, not a round number. Costs ≈ 0.6%/leg, options move ~2.5% on a typical winner → set X so the *option* clears cost (≈0.10–0.20% underlying / ~70–110 pts; tune it). Express in **% not raw points** (level-invariant).
- **N = 3** to start. The "first 3 bars in-direction" condition is what filters chop — it forces a *clean start*, not a round-trip range.
- Train **two heads** (clean-up, clean-down). This couples entry + direction on a *consistent* target and gives the engine a `CE_wins` / `PE_wins` probability directly.

### Variants to sweep (pick the most learnable)
| Variant | First-N condition | Note |
|---|---|---|
| **strict** | all 3 bars in-direction | cleanest, fewest positives |
| **soft** | 2-of-3 OR net-positive over first 3 | more samples, more robust — **fallback if strict is too sparse** |
| **efficiency** | `\|net\| / path >= 0.6` over Y | continuous "cleanliness" |

> Existing structure to reuse: the **dual-direction bundle** (`strategy_app/ml/...`, `_resolve_direction_dual`, `ce_bundle`/`pe_bundle`) — the two-head model already has a home in the engine.

### What this buys us
- Model learns **trend-vs-chop** from features (a hand-coded regime gate can't).
- "Clean start" is in the **label**, so the model predicts it at T → **enter immediately, no late-entry cost.**
- Fires **less often** (clean trends are rarer) — that's intended: *"big move, big profit, but less."*

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

A published **dual-head entry/direction bundle** that, on **true OOS**, is **net-positive after cost** and **survives drop-outlier robustness** (not carried by 1–2 trades), with calibrated probabilities. Then strategy wires it into the live engine (paper → validate → live).

**Questions / context:** see `docs/ENGINE_DECISION_FLOW.md` (how the live engine consumes the model) and the memory notes on the direction findings.
