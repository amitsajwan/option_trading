# Models Index — BankNifty option-trading brain

_Living index of every model in the system: what it predicts, its label, features, status, and what each still needs. Last updated 2026-06-11._

## The two jobs (read this first)
The system deliberately **separates two questions**, because they have very different ceilings:
- **ENTRY (magnitude):** *will a move happen?* — SOLVED, AUC ~0.82–0.83. Direction-agnostic.
- **DIRECTION:** *which way?* — the bottleneck, ~0.59 AUC / ~56% ceiling, and **regime-dependent** (2024 trended → momentum worked; 2026-recent mean-reverts → fade works). See `MEMORY.md` direction entries.

A model can only be as good as the information in its features. Relabeling the entry model to predict direction (clean-move, continuation) has been **refuted** (AUC collapses to 0.49) — direction needs either the **agreement-lever** (subset abstain, ~61%) or **new information** (news/grounding).

---

## A. Entry magnitude models (direction-agnostic: "a move ≥ X% in Y min")

| Model | Label (X% / Y / dir) | Features | Status | Holdout AUC | Location |
|---|---|---|---|---|---|
| `entry_only_v3` | 0.20% / 5m / either | ~51 (fo subset) | live-capable, prior baseline | 0.831 | GCS `*_v3/active` |
| **comprehensive 010pct** | 0.10% (~55pt) / 5m / either | **57** (`fo_comprehensive`) | ✅ **published, ALL_PASS** | **0.821** | VM `published_comprehensive/entry_only_model_010pct.joblib` |
| **comprehensive 013pct** | 0.13% (~70pt) / 5m / either | 57 (`fo_comprehensive`) | ✅ **published, ALL_PASS** | **0.824** | `published_comprehensive/...013pct.joblib` |
| **comprehensive 020pct** | 0.20% (~110pt) / 5m / either | 57 (`fo_comprehensive`) | ⏳ **training** (final magnitude sweep, ETA ~tonight) | — | (pending) |

- **Selected model so far:** `xgb_regularized`. Calibrated (isotonic, valid 2024-05..07), OOS 2024-08..10, drop-outlier robust.
- Runtime contract: `kind="entry_only_bundle"`, keys `features` / `feature_medians` / `model`; loaded via `ENTRY_ML_MODEL_PATH`, threshold `ENTRY_ML_MIN_PROB`. Feature row built by `project_stage_views_v2` (must be live-computable).

## B. Direction models ("which way / CE vs PE")

| Model | Label | Features | Status | AUC | Notes |
|---|---|---|---|---|---|
| `direction_only_v2` | CE beats PE (3m) | fo subset | published GCS, not live | 0.593 | thin pocket ~64% @prob≥0.60 (20% cov); ≈0.5 on bars we actually trade |
| `ce_win_v1` / `pe_win_v1` (dual, 05-23) | signed win | fo | **speced, training never finished** | — | superseded by Section C |
| 3-signal agreement lever | momentum+max_pain+OI agree, 10m big moves | rule (not ML) | **validated ~61%**, not wired | — | the one real direction edge; use as **abstain gate** |

## C. Dual SIGNED entry models — NEW (your "two models, regime picks which") ⏳ queued

| Model | Label | Features | Status | What it needs |
|---|---|---|---|---|
| **dual CE** `entry_s1_dual_ce_5m_013pct` | **fwd-5m HIGH ≥ +0.13%** (`entry_bn_5m_up_v1`) | `fo_comprehensive` | 🟡 **queued tonight** (auto after magnitude) | see below |
| **dual PE** `entry_s1_dual_pe_5m_013pct` | **fwd-5m LOW ≤ −0.13%** (`entry_bn_5m_down_v1`) | `fo_comprehensive` | 🟡 **queued tonight** | see below |

- **Output:** a calibrated probability per side; the model does **not** say CE/PE — **direction is supplied live by regime**, then we fire the matching side if its prob clears the threshold. Bundle records `side` / `direction` for runtime.
- **Auto-publishes** when gates pass (no hold). Orchestrator: `ops/gcp/run_dual_entry_retrain_vm.sh`.
- **What extra these need to actually work (beyond what's built):**
  1. **Regime features** in the view (realized-vol state, trend-vs-range, dist-from-VWAP/max_pain, time-of-day) so the model learns the **follow-vs-fade flip**. *Current build uses `fo_comprehensive` only — regime features are the first upgrade if v1 underwhelms.*
  2. **A live regime classifier / switch** (bull→CE, bear→PE, or consult-both) — this is the hinge; measured today that naive flow-following loses (43%) and fade wins (57%) in the current regime.
  3. **Runtime dual-bundle wiring** + a regime gate in `ml_entry.py` (today only a single `ENTRY_ML_MODEL_PATH` is loaded).
  4. **Honest eval:** per-side AUC, **follow-through on fired bars**, **per-year** (so it isn't just fitting 2026 reversion).

## D. LLM / oversight (shadow only)
| Component | Status | Result |
|---|---|---|
| Oversight brain (Groq) | deployed, gate OFF | no P&L edge (fired zero vetoes); shadow only |
| LLM direction picker (Groq llama-3.3-70b) | tested | 56.9% ≈ structural ceiling — **refuted** as edge |
| Gemini web-grounding (news/RBI) | **not built** | the only live bet to lift the direction ceiling (NEW information) |

---

## Refuted approaches (don't re-run without new information)
- Clean-move / monotonic-3-bar entry label → AUC 0.83→**0.49** (embeds the direction coin-flip).
- 2–5min momentum **continuation** → **anti-predictive** (~47%); confirmation makes it worse.
- Flow-following direction (VWAP/EMA align) → **43%** on big moves recently (fade = 57%).
- Lottery exit stack; E1–E8 long-ATM-1min arc (zero OOS edge).
