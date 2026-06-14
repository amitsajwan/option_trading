# Models Index — BankNifty option-trading brain

_Living index of every model in the system: what it predicts, its label, features, status, and what each still needs. Last updated 2026-06-14._

## The two jobs (read this first)
The system deliberately **separates two questions**, because they have very different ceilings:
- **ENTRY (magnitude):** *will a move happen?* — SOLVED, AUC ~0.82–0.83. Direction-agnostic.
- **DIRECTION:** *which way?* — the bottleneck, ~0.59 AUC / ~56% ceiling, and **regime-dependent** (2024 trended → momentum worked; 2026-recent mean-reverts → fade works). See `MEMORY.md` direction entries.

A model can only be as good as the information in its features. Relabeling the entry model to predict direction (clean-move, continuation) has been **refuted** (AUC collapses to 0.49) — direction needs either the **agreement-lever** (subset abstain, ~61%) or **new information** (news/grounding).

---

## A. Entry magnitude models (direction-agnostic: "a move ≥ X% in Y min")

| Model | Label (X% / Y / dir) | Features | Status | Holdout AUC | Location |
|---|---|---|---|---|---|
| **`entry_only_v3`** | 0.20% / 5m / either | ~51 (fo subset) | ✅ **ACTIVE — live-capable, deployed** | 0.831 | GCS `published_models/entry_only_v3/` |
| comprehensive 010pct | 0.10% (~55pt) / 5m / either | 57 (`fo_comprehensive`) | published, not deployed (superceded by v3) | 0.821 | GCS `published_comprehensive/` |
| comprehensive 013pct | 0.13% (~70pt) / 5m / either | 57 (`fo_comprehensive`) | published, not deployed | 0.824 | GCS `published_comprehensive/` |
| comprehensive 020pct | 0.20% (~110pt) / 5m / either | 57 (`fo_comprehensive`) | not pursued (v3 sufficient) | — | — |

- **Active model:** `entry_only_v3` (xgb_shallow). Calibrated, ECE 0.009, ship_gates_all_pass=True.
- **Correct threshold:** `ENTRY_ML_MIN_PROB=0.45` (fire_rate 0.79%, precision 59%). ⚠️ Live .env.compose has wrong 0.85 — needs manual fix.
- **Correct path in container:** `/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib`. ⚠️ Live .env.compose has wrong `/app/models/` — vol gate silently OFF until fixed.
- **Repurpose:** use as "don't-sell-into-a-move" gate for S3 seller — skip iron condor if model fires high magnitude probability.
- Runtime contract: `kind="entry_only_bundle"`, keys `features` / `feature_medians` / `model`; loaded via `ENTRY_ML_MODEL_PATH`, threshold `ENTRY_ML_MIN_PROB`. Feature row built by `project_stage_views_v2`.

## B. Direction models — ABANDONED (direction is ~50% coin flip)

> Direction has been conclusively refuted: quorum 50.3% in 2024 over 37k bars, inverts to 43.9% in 2026 OOS.
> Do NOT build or deploy direction models for buy-side. See `FINDINGS_2026-06-14.md`.

| Model | AUC | Verdict |
|---|---|---|
| `direction_only_v2` | 0.593 | ❌ in-sample; live inverts to 43.9% — RETIRED, do not deploy |
| Dual CE/PE (`entry_s1_dual_*`) | never finished | ❌ abandoned — same ceiling applies |
| 3-signal agreement lever (rule) | ~61% on big moves | Partially validated, but inverts OOS 2026; not deployed |

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
