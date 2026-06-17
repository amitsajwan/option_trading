# BMM — Handover & "Here → Prod" Runbook

_Owner away. This is everything another engineer needs to take the compression Big-Move
Model (BMM) entry detector from its current state to production. Self-contained; read top to
bottom. Branch: **`feat/compression-state-engine`**. Date: 2026-06-17. **Real money is OFF
(paper) — keep it that way until the explicit go-live gate below.**_

Related docs: [BMM_RESULTS.md](BMM_RESULTS.md) (verdict/numbers) ·
[BMM_PROPOSAL_REVIEW_BRIEF.md](BMM_PROPOSAL_REVIEW_BRIEF.md) (origin) ·
[SYSTEM_FLOW.md](SYSTEM_FLOW.md) (how a bar becomes a trade) ·
[strategy_platform/CONFIG.md](strategy_platform/CONFIG.md) (config = one source) ·
[MODELS_INDEX.md](MODELS_INDEX.md).

---

## 0. TL;DR (read this first)

- **What:** we added a compression/stored-energy/structure feature group to the entry
  "will-a-move-happen" model. One shared module computes them for **both** live and historical
  → **zero train/serve skew**.
- **Verdict:** compression features add **+0.009 AUC** (small but real). The multi-horizon
  models all select the **same nested entries** (corr 0.92–0.99) → **ship ONE model**, threshold
  = selectivity. Move-detection is **saturated**; this does **not** move P&L.
- **Decision (from product owner):** go with the new compression model; **single model**;
  **ML-only entry gate** (drop the ATR-OR), wrapped in the Selection Gate with
  freshness-abstain.
- **In flight:** training the **production** model = `fo_bmm_v1` @ 5m/0.20% on the **v2 view**
  (what live actually emits → serve-parity). One run decides ship-vs-keep-v3 (0.831).
- **The real lever is still DIRECTION**, not entry. Entry is "done right" now; profitability
  is gated on the direction Stage-2 experiment (§7).

---

## 1. Current state (as of handover)

| Piece | State |
|---|---|
| Compression feature module (`snapshot_app/core/compression_features.py`) | ✅ committed, live+historical wired |
| `fo_bmm_v1` feature set + Lean-5 + A/B configs | ✅ committed |
| Lean-5 grid trained (h05–h30) | ✅ done — AUC 0.76–0.79 |
| A/B (compression vs baseline @5m/0.20%) | ✅ done — **+0.009** (0.815 vs 0.806) |
| Nesting analysis (entries are the same signal) | ✅ done — corr 0.92–0.99 |
| `docs/BMM_RESULTS.md` verdict | ✅ committed |
| Production config `bmm_prod_5m020_v2view` | ✅ committed (not yet trained) |
| Enriched **v2 view** uploaded to GCS for VM training | 🔄 **in progress** (`gs://amit-trading-option-trading-snapshots/bmm_v2view/`) |
| Production model trained on v2 view | ⏳ **next action (§4 Step 1)** |
| Bundle export + ML-only gate wiring | ⏳ pending (§5–§6) |
| SIM validation + live deploy | ⏳ pending (§8–§9) |
| Direction Stage-2 (profit lever) | ⏳ pending (§7) |

**Everything is committed and pushed** to `origin/feat/compression-state-engine`. Nothing
important lives only on a VM.

---

## 2. Environment & access (reference)

- **ML/training VM:** `option-trading-ml-01`, zone `asia-south1-b`, project `amit-trading`.
  - `gcloud compute ssh option-trading-ml-01 --zone=asia-south1-b`
  - SSH is **flaky** — retry once on failure.
  - 8 cores / 31 GB. **No `python` on PATH — use `python3`.**
  - Training runs in a **git worktree** `~/bmm_run` (so it never disturbs `/opt/option_trading`,
    which holds uncommitted brain/seller work on `feat/intelligent-brain`). Run with
    `PYTHONPATH=~/bmm_run/ml_pipeline_2/src:~/bmm_run`.
  - Data: `~/parquet_data/` — has `snapshots_ml_flat_v2`, `stage1_entry_view_v3_candidate`
    (enriched). **No `market_base`, no `stage1_entry_view_v2`** (that's why we ship the v2 view
    via GCS, see §4).
  - Deps installed `--user`: lightgbm, optuna (others were present).
  - tmux sessions used: `bmm` (Lean-5, done), `bmm2` (h15m), `ab` (A/B). New runs: make a new
    session.
- **Live/runtime VM:** SEPARATE machine (the GCP runtime that runs `strategy_app` /
  `execution_app` in Docker). Do not confuse with the ML VM. See `MEMORY` / `CONFIG.md`.
- **GCS:** `gs://amit-trading-option-trading-models/` (published bundles),
  `gs://amit-trading-option-trading-snapshots/` (data staging — we used `bmm_v2view/`).
- **Git:** branch `feat/compression-state-engine`. The VM worktree updates via
  `git -C /opt/option_trading fetch origin feat/compression-state-engine` then
  `git -C ~/bmm_run reset --hard origin/feat/compression-state-engine`.

---

## 3. What we learned (so you don't re-run it)

1. **Single model only.** The 4 horizons correlate 0.92–0.99 and nest perfectly
   (`P(short fires | long fires)=1.00`). Multiple models = redundant. Threshold controls
   selectivity. (Tool: score models on holdout, see `BMM_RESULTS.md §2`.)
2. **Compression = +0.009 AUC.** Real, small. Keep it *only if* the v2-view model ≥ v3.
3. **The v3 "gap" was the view, not the features** — candidate view is ~0.025 weaker than v2.
   That's why production trains on v2.
4. **Entry/move-detection is saturated and ≈ ATR.** It is NOT the bottleneck and does NOT
   move P&L. Do not spend more cycles here.
5. **OOM is the main operational risk** — 5 parallel HPO jobs OOM-killed one (6.8 GB RSS each).
   Run ≤3 heavy jobs at once on the 8-core/31 GB box, or `model_n_jobs` low.

---

## 4. HERE → PROD, step by step

### Step 1 — Train the production model (the deciding run)

The enriched **v2 view** is uploading to `gs://amit-trading-option-trading-snapshots/bmm_v2view/`.
Once upload completes (~1198 parquet files):

```bash
ZONE=asia-south1-b
gcloud compute ssh option-trading-ml-01 --zone=$ZONE
# on the VM:
gsutil -m rsync -r gs://amit-trading-option-trading-snapshots/bmm_v2view/ \
    ~/parquet_data/stage1_entry_view_v2/        # config's stage1_view_id resolves here
cd ~/bmm_run
git fetch origin feat/compression-state-engine && git reset --hard origin/feat/compression-state-engine
export PYTHONPATH=~/bmm_run/ml_pipeline_2/src:~/bmm_run
tmux new -d -s bmmprod \
 'python3 -m ml_pipeline_2.run_research \
   --config ml_pipeline_2/configs/research/staged_dual_recipe.bmm_prod_5m020_v2view.json \
   > ~/bmm_logs/bmm_prod_5m020_v2view.log 2>&1'
# watch:
python3 ml_pipeline_2/scripts/bmm_results.py    # add this run name if needed, or grep the log
```

**Decision gate:** read stage1 `roc_auc` from the final JSON in the log.
- **AUC > 0.831** → this is the model. Proceed to Step 2.
- **AUC ≤ 0.831** → flag it. Per the product owner we still go with the compression model, but
  surface that it didn't beat v3 so the call is made with eyes open. (Default fallback if the
  team wants proven: keep `entry_only_v3`.)

> Note: the v2 stage2/stage3 view dirs are symlinked to the candidate view on the VM to satisfy
> manifest validation (stage2/3 are bypassed — `entry_only_publish`). If validation complains,
> recreate: `ln -sfn ~/parquet_data/stage1_entry_view_v2 ~/parquet_data/stage2_direction_view_v2`
> (and `stage3_recipe_view_v2`).

### Step 2 — Export & calibrate the bundle

The research run with `entry_only_publish:true` produces a model package under
`~/bmm_run/ml_pipeline_2/artifacts/research/<run>_<ts>/stages/stage1/model.joblib`
(bundle dict: `feature_columns`, `models['move']` = sklearn Pipeline, `_model_input_contract`).

Convert to the runtime `entry_only_bundle` contract + isotonic calibration using the existing
scripts (mirror how `entry_only_v3` was published):
- `ml_pipeline_2/scripts/export_entry_bundle_from_research.py`
- `ml_pipeline_2/scripts/publish_entry_calibrated.py`

Verify the published bundle has keys `features` / `feature_medians` / `model` and that
`features` ⊆ the columns the **live v2 projection** emits (it does — compression cols are in the
`futures_derived` block of `project_stage_views_v2`). Upload to
`gs://amit-trading-option-trading-models/published_models/entry_bmm_v1/`.

### Step 3 — Wire the ML-only entry gate (code + config)

Product decision: **ML-only**, drop the ATR-OR, wrap in the Selection Gate, freshness→abstain.

- **Config (`.env.compose`):**
  - `ENTRY_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/entry_only/published/entry_bmm_v1.joblib`
    (container path; ensure the bundle is baked/bind-mounted there).
  - `ENTRY_ML_MIN_PROB=` set from the run's selected operating threshold (the threshold-sweep
    picks it; v3 used 0.45). Use the calibrated operating point.
  - `ENTRY_VOL_GATE_ENABLED=0` **only if** you are fully replacing the ATR backbone with ML.
    Prefer: keep the Selection Gate ON (`OPPORTUNITY_GATE_ENABLED=1`) and let ML prob be its
    primary signal (`w_entry_prob` already 0.60 in `opportunity.py`).
- **Code (entry gate):** in `strategy_app/engines/strategies/{ml_entry,vol_gate_entry}.py` /
  `deterministic_rule_engine._process_entry_votes`, ensure the path is **ML prob → Selection
  Gate**, and that a **stale/NaN feature row → abstain** (do NOT fall back to an ATR trade).
  The freshness gate ids already exist (`feature_freshness_v1`, `feature_completeness_v1`).
- This keeps the ATR only as an *unused* legacy path; the live decision is the calibrated ML
  prob ranked relative to today + cost floor (~108pt) + ≤3/day budget.

### Step 4 — ⚠️ Serve-parity: deploy the CODE, not just the model

**This is the critical landmine.** The live model only gets correct compression features if the
live container runs the branch code that **computes** them. The 12 compression columns are
produced by:
- `snapshot_app/core/compression_features.py` (shared module),
- `snapshot_app/core/market_snapshot.py` (`prepare_market_snapshot_window` → `futures_derived`),
- `snapshot_app/core/stage_views.py` (`futures_derived` block of the v2 projection spec).

If you deploy the bundle onto an OLD image, the model receives **NaN** for all 12 features →
silent degradation (this is exactly the "11:30 velocity" failure pattern we've hit before).
**So: rebuild/redeploy the `strategy_app` (and `dashboard`) images from this branch BEFORE or
WITH the model swap.** Verify after deploy (Step 5).

### Step 5 — Verify serve-parity live (must pass before trusting the model)

After deploy, on a live/replay snapshot confirm the model's feature row is non-NaN for the
compression columns:
```python
from snapshot_app.core.stage_views import project_stage_views_v2
row = project_stage_views_v2(snap.raw_payload)['stage1_entry_view_v2']
print({c: row.get(c) for c in
  ['bb_width_20','range_ratio_10_30','candle_overlap_10','ema_spread_9_21',
   'ema_order','position_in_day_range','compression_score']})
# all should be real numbers mid-session (NaN only in the first ~20-30 bars warmup)
```
Also confirm in the running container's startup log that the entry model path/threshold are the
new ones, and that the trade inspector shows entry votes with sane probs.

### Step 6 — SIM before live

Run the new model through the SIM (ops profile) on June-2026 days and confirm it fires sanely
(not 0, not every bar) and the inspector links vote→position. Use the shadow/prod profiles
(`ops/profiles/*.env`, `ops/run_sim_profile.sh`) per `CONFIG.md`. SIM must equal live (same code,
same bundle, same config).

---

## 5. The honest caveat (do not skip)

Everything above ships the **entry detector** correctly. It does **not** make the system
profitable. Across all our work, models that ace these entry/move gates (PF>3, MDD<3%, ~70%
move-hit) still come out **break-even in real option trading** because of **direction + the
~108pt round-trip cost**. **Do not turn on real money** on the strength of the BMM entry model.

---

## 6. THE actual next experiment — Direction Stage-2 (B3)

This is where the edge has to come from. Plan (per the proposer + our E1 finding):

- On the **move-positive subset** (bars the BMM fires on), measure direction accuracy:
  **follow-the-breakout vs FADE**. E1 already found breakout direction is anti-predictive →
  **fade ≈ 59%**. Confirm cost-aware (beat ~108pt), walk-forward (freeze 2020–23, test 2024),
  and **forward-check on 2026**.
- Reuse the existing council: `_regime_council_direction` in
  `strategy_app/engines/strategies/entry_direction_policy.py` (vwap+pcr+straddle, regime-gated,
  abstains). If fade-59% survives cost + forward → that's the headline; wire it as the Stage-2
  side-selector (else straddle / abstain).
- Harness pattern to copy: `research/compression_harness.py` (causal, per-month incremental,
  walk-forward) — build the analogous direction harness.

**If direction does not survive forward, the system stays non-directional** (straddle on the
move, or premium-selling — the only historically +EV path, see `MEMORY`).

---

## 7. Landmines / gotchas (learned the hard way)

- **OOM:** ≤3 heavy HPO jobs at once on the ML VM; each ~7 GB. One Lean-5 job got OOM-killed.
- **`python` vs `python3`:** VM has only `python3`.
- **Worktree, not `/opt` checkout:** never `git checkout` on `/opt/option_trading` (it has
  uncommitted brain/seller work + untracked files that block checkout). Use `~/bmm_run`.
- **`market_base` / v2 view absent on VM:** that's why we stage data via GCS.
- **Config = ONE source = `.env.compose`** (`docker compose --env-file .env.compose up -d`).
  Plain `up` without `--env-file` uses unsafe defaults and crashes (`capped_live` + size cap).
  See `CONFIG.md`. Image must be rebuilt so code/config are baked (no docker-cp drift).
- **Serve-parity (Step 4)** is the #1 way to silently ship a broken model.
- **SSH flaky:** retry once; for long jobs always use `tmux` (nohup dies on disconnect).

---

## 8. Key files & commands index

| Thing | Path |
|---|---|
| Shared feature module (live+historical) | `snapshot_app/core/compression_features.py` |
| Live computation | `snapshot_app/core/market_snapshot.py` (`prepare_market_snapshot_window`, `mss3`) |
| View projection spec | `snapshot_app/core/stage_views.py` (`futures_derived` block) |
| Historical rebuild | `snapshot_app/historical/rebuild_stage_views_from_flat.py` |
| Enrich an existing view | `ml_pipeline_2/scripts/enrich_view_compression.py` |
| Feature set | `fo_bmm_v1` in `ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py` |
| Production config | `ml_pipeline_2/configs/research/staged_dual_recipe.bmm_prod_5m020_v2view.json` |
| Grid/A-B configs | `…/staged_dual_recipe.bmm_*.json`, `…ab_5m020_*.json` |
| Config generator | `ml_pipeline_2/scripts/gen_bmm_configs.py` |
| Grid launcher | `ml_pipeline_2/scripts/run_bmm_grid.sh` |
| Results watcher | `ml_pipeline_2/scripts/bmm_results.py` |
| Verdict / numbers | `docs/BMM_RESULTS.md` |
| Run training | `python3 -m ml_pipeline_2.run_research --config <cfg>` (PYTHONPATH set) |

---

## 9. One-paragraph status to read aloud in standup

"The compression Big-Move-Model work is feature-complete and analyzed. Findings: compression
features add a small real lift (+0.009 AUC); the multi-horizon models are the same nested signal
so we ship one model with a threshold; entry/move-detection is saturated and doesn't move P&L.
Decision is to ship a single ML-only entry gate using the compression model, trained on the v2
view for serve-parity. The deciding production training run is queued (data staged to GCS). The
remaining path is: train → export+calibrate bundle → wire ML-only gate → **rebuild the image so
live computes the features** → verify serve-parity → SIM → deploy paper. Real money stays OFF.
The actual profit work — direction (fade) on the move-positive subset — has not started and is
the next real experiment."
