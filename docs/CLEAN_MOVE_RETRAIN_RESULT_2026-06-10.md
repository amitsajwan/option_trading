# Clean-Move Entry Retrain — Result (2026-06-10)

> **Verdict:** the clean-move entry label **as specified (5m / 0.12% / 3 clean bars) FAILS** — it has
> no learnable signal (baseline CV AUC ≈ 0.49, vs the magnitude label which passes the same gate).
> Entry stays on the existing magnitude model. Direction remains THE bottleneck. **Nothing published; real money OFF.**

This is the overnight execution of `docs/ML_RETRAIN_HANDOVER.md` (the ENTRY-only clean-move retrain).

## What ran
- ML VM `option-trading-ml-01` recreated from the training template (both VMs had been deleted); it
  self-configured (venv + 3.1 GB of 2020–2024 parquet rsync'd from GCS) and ran on branch
  `feat/intelligent-brain` (the clean-move label code is there, **not** on `main`).
- Two HPO manifests, entry-only: `entry_s1_clean_move_strict_hpo_v1` and `..._soft_hpo_v1`.
- Label: direction-agnostic `|Close(T+5m) − Close(T)| ≥ 0.12% AND the first 3 closes after T are
  monotone (strict) / mostly monotone (soft)`.
- Artifacts backed up to `gs://amit-trading-option-trading-snapshots/ml_pipeline/retrain_clean_move_20260610`.

## Result (the numbers)

| Run | label | stage1_cv ROC AUC | brier | half-split | drift | gate | publishable |
|---|---|---|---|---|---|---|---|
| clean-move **strict** | 5m / 0.12% / 3-clean | **0.4926** | 0.060 | 0.478 / 0.512 | 0.034 | **HELD** (`<0.52`) | no |
| clean-move **soft**   | 5m / 0.12% / 3-clean | **0.4926** (identical) | 0.060 | 0.478 / 0.512 | 0.034 | **HELD** | no |
| **control** 100pt magnitude (`entry_s1_only_hpo_v2`, label `move_barrier_hit`) | same pipeline + precheck | **PASSED** the gate (≥0.52) → proceeded to HPO | — | — | — | pass | — |
| (reference) published magnitude entry model, full eval | — | ~0.83 | — | — | — | — | live |

- CV rows ≈ 43,019; brier 0.060 ⇒ ~6 % positive rate ⇒ the label is **non-degenerate** (not a broken
  join). The model simply **cannot separate** clean-move-positive from negative bars — AUC ≈ random,
  both time-halves ~0.48/0.51.
- The **control is the clincher**: the *same* pipeline and *same* precheck gate, with only the label
  swapped to the 100pt magnitude label, **passes** the gate and trains. So the gate/metric is sound and
  discriminating — the failure is the **clean-move label itself**, not the harness.

## Why it fails (interpretation)
Requiring "the first 3 bars after entry are monotone" ≈ requiring **short-horizon directional
persistence**. We have independently established (3 ways) that **direction/persistence at this horizon is
a coin-flip**. So the clean-start condition folds an unpredictable target into the entry label and
**destroys the entry signal** (AUC 0.83 → 0.49). The handover's hypothesis — "clean-start makes the model
learn trend-vs-chop" — is **not supported**: the model can't learn it because it isn't there to learn.

**Implication:** entry should **stay on the existing magnitude model** (AUC ~0.83). The clean-move idea
does not rescue P&L. The bottleneck is unchanged and unmoved: **direction** (see the direction-phase notes
in the handover and the memory `project_entry_vs_direction_2026-06-08`).

## Two real defects found (worth fixing before any re-test)
1. **strict == soft is degenerate at `n_clean_bars=3`.** In `entry_move_oracle.py::_is_clean_start`,
   `required = strict: n−1 = 2` and `soft: ceil((n−1)·2/3) = ceil(1.33) = 2` — **identical**. With 3 closes
   there are only 2 inter-bar steps, so "2-of-2" == "all". The two runs trained the *same* label (byte-identical
   AUC). **The soft variant was never actually tested.** To differentiate strict vs soft, use `n_clean_bars ≥ 4`.
2. **`optuna` was missing from `ml_pipeline_2/pyproject.toml`.** `pip install -e ./ml_pipeline_2` on a fresh
   VM left it absent and every HPO died with `requires the 'optuna' package`. Fixed + pushed (commit `88e36c3`).

## Caveats (don't over-read)
- Only **one** `min_pct` (0.12%) and **one** horizon (5m) were tried; the gate aborted before HPO, so this is a
  **baseline-model** precheck, not a fully-tuned AUC. But a 0.49 baseline (both halves ~random) is not something
  HPO turns into an edge — the gate is doing its job.
- Control window (2022–2024) ≠ clean-move window (2020–2024). Minor; a 0.49 baseline = no signal regardless of window.

## If anyone wants to push further (not recommended before fixing direction)
- Re-run **soft with `n_clean_bars=4`** (so strict≠soft) and sweep `min_pct` ∈ {0.10%, 0.15%, 0.20%}.
- But the prior is strong: any "clean-start" / persistence condition will re-import the direction coin-flip.
  Effort is better spent on the **direction** model (independent microstructure signal stacked on `fade_r5m≈55%`).

## State
- ML VM `option-trading-ml-01`: training done; **stopped** to save cost (disk + data persist; restart with
  `gcloud compute instances start`). All results on the disk and in GCS.
- Nothing deployed, nothing published, real money OFF.
