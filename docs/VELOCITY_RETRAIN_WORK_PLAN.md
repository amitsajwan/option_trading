# Velocity Retrain — Full System Work Plan
> **Status**: Active  
> **Last updated**: 2026-04-12  
> **Trigger**: `snapshots_ml_flat_v2` enrichment complete (36 velocity + morning context features added)  
> **Goal**: Full 3-stage retrain on enriched dataset, HPO enabled, CE-only shadow deployed in parallel  

---

## Assumptions

- `snapshots_ml_flat_v2` is written to GCP at  
  `/home/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/`  
- Existing Stage 1 artifact (ROC 0.619) is **invalidated** — retrain from scratch with new features  
- Velocity columns: `vel_ce_oi_delta_*`, `vel_pcr_*`, `vel_price_*`, `vel_iv_*`, `vel_volume_*`, `ctx_am_*` (36 total)  
- HPO strategy by phase:
  - **Screen** (`velocity_screen_v1`): `trials=5, strategy=random` — enough to avoid false eliminations, cheap for 12 lanes
  - **HPO run** (`velocity_hpo_v1`): `trials=30, strategy=optuna` — TPE Bayesian search on top-3 survivors
  - **Final model**: `trials=50, strategy=optuna` — full squeeze on single winner  
- New features to add: ADX, volume spike ratio, gap flag  

---

## Team Structure & Workstreams

```
Team A — Data Engineering      (depends on: enrichment VM finish)
Team B — Feature Engineering   (no dependencies — starts immediately)
Team C — ML Pipeline Eng       (no dependencies — starts immediately)
Team D — ML Research / Config  (depends on B + C done)
Team E — Infrastructure        (depends on D config ready)
```

---

## Team A — Data Engineering

**Owner**: Data Engineering  
**Starts**: When enrichment VM shows 100% complete  
**Estimated effort**: 1 day  

### Tasks

#### A1 — Validate `snapshots_ml_flat_v2` output
Run contract validation on the written dataset:
```bash
python -m snapshot_app.core.snapshot_ml_flat_contract \
    --dataset snapshots_ml_flat_v2 \
    --parquet-root /home/.data/ml_pipeline/parquet_data \
    --schema-version 4.0
```
Check:
- All 36 `vel_` and `ctx_am_` columns present  
- No columns with missing rate > 5% (velocity columns allowed up to 15% for edge dates)  
- Row count ≥ original `snapshots_ml_flat` row count  
- Year partitions: 2020, 2021, 2022, 2023, 2024 all present  

**Definition of Done**: Validation script exits 0, summary JSON written to  
`/tmp/v2_validation_report.json` with `"status": "pass"`.

---

#### A2 — Spot-check 3 sample dates
For 3 random dates (one per year: 2021, 2022, 2023), manually confirm:
```bash
python -m snapshot_app.historical.enrichment_runner \
    --dry-run \
    --start-date 2022-06-15 \
    --end-date 2022-06-15 \
    --parquet-root /home/.data/ml_pipeline/parquet_data \
    --output-dataset snapshots_ml_flat_v2_spot
```
Confirm: `vel_ce_oi_delta_open`, `vel_price_momentum_5m`, `ctx_am_trend` are non-null  
and within expected ranges.

**Definition of Done**: 3 spot-check reports written, all `errors=0`, `no_morning=0` for sample dates.

---

#### A3 — Promote dataset
Only after A1 and A2 pass:
```bash
# On GCP — rename v2 to production name
mv /home/.data/ml_pipeline/parquet_data/snapshots_ml_flat \
   /home/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v1_archive

mv /home/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2 \
   /home/.data/ml_pipeline/parquet_data/snapshots_ml_flat
```
**DO NOT delete `snapshots_ml_flat_v1_archive`** — keep until first campaign completes.

**Definition of Done**: `ls parquet_data/snapshots_ml_flat/` shows year=2020..2024 directories.  
`ls parquet_data/snapshots_ml_flat_v1_archive/` also exists as fallback.

---

#### A4 — Shut down enrichment VM
After A3 complete:
```bash
gcloud compute instances stop option-trading-snapshot-enrichment-01 \
    --zone=asia-south1-b --project=amittrading
```
**Definition of Done**: VM status = `TERMINATED` in `gcloud compute instances list`.

---

## Team B — Feature Engineering

**Owner**: ML Engineering  
**Starts**: Immediately (no dependencies)  
**Estimated effort**: 1 day  

### Tasks

#### B1 — Add `vel_` and `ctx_am_` to direction feature set
In [ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py](ml_pipeline_2/src/ml_pipeline_2/catalog/feature_sets.py), update `fo_midday_direction_regime_v1`:

```python
# Add these regex entries to fo_midday_direction_regime_v1:
r"^vel_ce_oi_delta_",          # CE OI velocity (open, 15m, 30m, 60m)
r"^vel_pe_oi_delta_",          # PE OI velocity
r"^vel_oi_imbalance_",         # OI imbalance shift
r"^vel_pcr_",                  # PCR velocity
r"^vel_price_momentum_",       # Price momentum (5m, 15m, 30m)
r"^vel_iv_compression_",       # IV compression rate
r"^ctx_am_trend$",             # Morning session trend (UP/DOWN/FLAT)
r"^ctx_am_price_position$",    # Where price sits vs AM range
r"^ctx_am_oi_build_",          # AM OI build direction
r"^ctx_am_volume_",            # AM volume profile
```

Also create a new **velocity-only** feature set for Stage 1 screening:
```python
FeatureSetSpec(
    name="fo_velocity_v1",
    include_regex=(
        r"^vel_",
        r"^ctx_am_",
        r"^ema_",
        r"^vwap_distance$",
        r"^osc_rsi_14$",
        r"^osc_atr_",
        r"^ctx_dte_days$",
        r"^ctx_is_expiry_day$",
        r"^ctx_is_near_expiry$",
        r"^minute_of_day$",
    ),
),
```

**Definition of Done**: `feature_set_specs_by_name()` returns `fo_midday_direction_regime_v1` 
with velocity entries, and `fo_velocity_v1` as a new key. Tests pass:
```bash
cd ml_pipeline_2 && python -m pytest tests/test_staged_grid.py::test_midday_redesign_feature_sets_are_registered -v
```

---

#### B2 — Add 3 missing features to snapshot contract + compute

**Feature 1: ADX (trend quality)**  
Add to `snapshot_app/core/velocity_features.py`:
```python
def _compute_adx(highs, lows, closes, period=14):
    """Average Directional Index — measures trend strength 0-100."""
    # tr, +dm, -dm → smoothed → DI+, DI- → DX → ADX
    # Returns float in [0, 100]
```
Column name: `adx_14`  
Range: 0–100. ADX > 25 = trending, < 20 = ranging.

**Feature 2: Volume spike ratio**  
```python
# current_volume / rolling_20d_avg_volume
# Column: vol_spike_ratio
# Range: 0.0–10.0 (>2.0 = spike)
```

**Feature 3: Gap flag**  
```python
# (open_price - prev_close) / prev_close
# Column: ctx_gap_pct   → float, range roughly -0.05 to +0.05
# Column: ctx_gap_up    → bool (gap_pct > +0.003)
# Column: ctx_gap_down  → bool (gap_pct < -0.003)
```

Add all 4 new columns to `REQUIRED_COLUMNS_V2` in `snapshot_ml_flat_contract.py`  
and to `validation_rules.yaml`.

**Definition of Done**:  
- `compute_velocity_features()` returns dataframe with `adx_14`, `vol_spike_ratio`,  
  `ctx_gap_pct`, `ctx_gap_up`, `ctx_gap_down` for valid input  
- Unit tests pass in `snapshot_app/tests/test_velocity_features.py`  
- `fo_velocity_v1` includes `r"^adx_14$"`, `r"^vol_spike_ratio$"`, `r"^ctx_gap_"`  

---

#### B3 — Update test assertions
In `ml_pipeline_2/tests/test_staged_grid.py`, add:
```python
assert "fo_velocity_v1" in specs
assert "fo_midday_direction_regime_v1" in specs
# Spot-check velocity patterns are in v1
spec = specs["fo_midday_direction_regime_v1"]
patterns = spec.include_regex
assert any("vel_" in p for p in patterns)
```

**Definition of Done**: `pytest ml_pipeline_2/tests/test_staged_grid.py -v` → all pass.

---

## Team C — ML Pipeline Engineering

**Owner**: ML Engineering  
**Starts**: Immediately (no dependencies)  
**Estimated effort**: 2 days  

### Tasks

#### C1 — Implement `bypass_stage2` flag
In `ml_pipeline_2/src/ml_pipeline_2/staged/pipeline.py`, add support for 
CE-only mode where Stage 2 is skipped entirely:

In the manifest `training` section:
```json
"bypass_stage2": true
```

Pipeline behaviour when `bypass_stage2: true`:
- Skip Stage 2 training entirely  
- All Stage 1 TRADE entries are routed directly to Stage 3  
- `direction_label` forced to `CE` for all trades (BankNifty structural CE bias)  
- Stage 3 runs on CE recipes only  

**Definition of Done**:  
- `bypass_stage2: true` in manifest → pipeline completes Stage 1 then Stage 3 (no Stage 2 artifacts written)  
- `bypass_stage2: false` (default) → existing 3-stage behaviour unchanged  
- Test: `pytest ml_pipeline_2/tests/ -k bypass_stage2`

---

#### C2 — Enable proper HPO (trials_per_model: 30)
Our current HPO is `strategy: "random", trials_per_model: 1` — effectively no search.

Create a new HPO-enabled base manifest:  
`ml_pipeline_2/configs/research/staged_dual_recipe.velocity_hpo_v1.json`

Key changes vs `staged_dual_recipe.stage2_hpo.json`:
```json
"search_options_by_stage": {
    "stage1": {
        "hpo": {
            "enabled": true,
            "strategy": "random",
            "trials_per_model": 20,
            "sampler_seed": 42
        }
    },
    "stage2": {
        "hpo": {
            "enabled": true,
            "strategy": "random",
            "trials_per_model": 30,
            "sampler_seed": 42
        }
    },
    "stage3": {
        "hpo": {
            "enabled": true,
            "strategy": "random",
            "trials_per_model": 15,
            "sampler_seed": 42
        }
    }
},
```
Model catalog for each stage:
- Stage 1: full XGB + LGBM suite (as in `stage2_hpo.json`)  
- Stage 2: XGB + LGBM + LogReg  
- Stage 3: XGB + LGBM + LogReg  

Dataset: `"support_dataset": "snapshots_ml_flat"` (after promotion by Team A).

**Definition of Done**:  
- Manifest validates via `validate_manifest()`  
- HPO config shows `enabled: true` per stage  
- `trials_per_model` ≥ 20 for Stage 2  

---

#### C3 — Create CE-only manifest config
`ml_pipeline_2/configs/research/staged_dual_recipe.ce_only_v1.json`

```json
{
  "inputs": { "support_dataset": "snapshots_ml_flat" },
  "training": {
    "bypass_stage2": true,
    "stage1": { ... full catalog },
    "stage3": { ... full catalog + HPO enabled }
  },
  "policy": {
    "stage1_policy_id": "entry_threshold_v1",
    "stage3_policy_id": "recipe_top_margin_v1"
  }
}
```

**Definition of Done**: Manifest loads without errors, `bypass_stage2: true` confirmed.

---

#### C4 — Wiring check: velocity columns flow through to model training
Write an integration test that:
1. Loads 30 days of `snapshots_ml_flat_v2` (or fixture)  
2. Resolves `fo_velocity_v1` feature set spec  
3. Confirms `vel_ce_oi_delta_open`, `ctx_am_trend`, `adx_14` are in the resolved column list  
4. Confirms no KeyError during `_build_stage2_frame()`  

```bash
pytest ml_pipeline_2/tests/test_velocity_wiring.py -v
```

**Definition of Done**: Test passes on dev machine against 30-day sample from v2 dataset.

---

## Team D — ML Research / Experiment Design

**Owner**: ML Research  
**Starts**: After B1 + C2 complete  
**Estimated effort**: 1 day config + GCP run time  

### Tasks

#### D1 — Design velocity screen campaign
New campaign: `velocity_screen_campaign_v1`  
Purpose: fast screen to find which velocity feature families have Stage 2 signal.

Approach: same pattern as `stage2_family_screen_campaign_v2` but with velocity feature sets.

Feature families to screen (12 lanes):
| Lane | Feature set | Hypothesis |
|------|-------------|-----------|
| 1 | `fo_velocity_v1` (all vel) | Kitchen sink |
| 2 | `fo_velocity_v1` + midday filter | Velocity signal stronger midday |
| 3 | OI velocity only (`vel_ce_oi_`, `vel_pe_oi_`) | OI flow is the key |
| 4 | Price velocity only (`vel_price_`) | Momentum matters more |
| 5 | IV velocity only (`vel_iv_`) | IV compression predicts direction |
| 6 | Morning context only (`ctx_am_`) | AM session predicts MIDDAY |
| 7 | vel_ + ADX (trend quality filter) | Only trade when trending |
| 8 | vel_ + vol_spike_ratio | Volume confirms direction |
| 9 | vel_ + gap flag | Gap direction persists |
| 10 | `fo_midday_direction_regime_v1` (with vel_) | Full v1 + velocity |
| 11 | `fo_midday_direction_regime_v1` 5d oracle | Short regime memory |
| 12 | Momentum composite (pcr_vel + price_vel + oi_imbalance) | Combined momentum |

Gate: same as screen v2 — Stage 2 signal check first, then CV gate.

**Definition of Done**: `velocity_screen_campaign_v1.json` created, validates,  
`campaign_max_lanes: 12`, each lane has correct `stage2_feature_families` mapping.

---

#### D2 — Design full retrain campaign (post-screen)
After screen identifies ≥ 3 lanes with `drift < 0.08`:

New campaign: `velocity_full_retrain_v1`  

Structure:
- 4-year window (`canonical_4y`)  
- Top-3 velocity feature sets from screen  
- Full model catalog per stage (Stage 1, 2, 3)  
- HPO enabled (`velocity_hpo_v1` as base manifest)  
- Stage 1: **retrain from scratch** (do NOT reuse old Stage 1 artifact)  
- Stage 3: full recipe catalog `midday_l3_adjacent_v1`  

**Definition of Done**: Campaign config created and validates. `reuse_stage1_from` is NOT set  
(full retrain). `max_generated_lanes ≤ 6` (resource budget).

---

#### D3 — Design CE-only campaign
Separate campaign: `ce_only_retrain_v1`  
Uses `staged_dual_recipe.ce_only_v1.json` from C3.  
Single lane, 4-year window.  

This runs **in parallel** with the velocity screen — no dependency.

**Definition of Done**: `ce_only_retrain_v1.json` created. Single lane, `bypass_stage2: true`.

---

#### D4 — Feature signal diagnostic on velocity features
Before launching D2, run the signal check:
```bash
python -m ml_pipeline_2.staged.stage2_feature_signal \
    --run-dir ml_pipeline_2/artifacts/campaign_runs/velocity_screen_campaign_v1/lanes/[best_lane]/runner_output \
    --output /tmp/velocity_signal_check.json
```

Gate: `n_cross_window_stable_features >= 3`  

**Definition of Done**: Signal check JSON written. If passes → launch D2. If fails → report to team lead, pivot to CE-only only.

---

## Team E — Infrastructure

**Owner**: DevOps / Infra  
**Starts**: After D1 config ready  
**Estimated effort**: 0.5 days  

### Tasks

#### E1 — Resource check before full retrain
The full retrain campaign will use HPO (trials_per_model: 30) which is ~30x more compute than the current screen.

Estimate per lane:
- Screen: ~4 hrs/lane  
- Full retrain with HPO: ~6–8 hrs/lane × 6 lanes = ~36–48 hrs GCP time  
- Cost: n2-highmem-8 at ~$0.50/hr → ~$20–25 total  

Confirm before launch:
```bash
gcloud compute instances describe option-trading-snapshot-build-01 \
    --zone=asia-south1-b --project=amittrading \
    --format="get(status,machineType)"
```

**Definition of Done**: VM confirmed RUNNING, disk > 50GB free, no other jobs running.

---

#### E2 — Launch velocity screen
```bash
# On GCP
cd ~/option_trading/ml_pipeline_2
python -m ml_pipeline_2.run_campaign \
    configs/campaign/velocity_screen_campaign_v1.json \
    --log-level INFO &> logs/velocity_screen.log &
```

**Definition of Done**: `workflow_state.json` shows `status: running`, lane 1 starts within 5 min.

---

#### E3 — Launch CE-only retrain (parallel)
```bash
cd ~/option_trading/ml_pipeline_2
python -m ml_pipeline_2.run_staged_grid \
    configs/research/staged_grid.ce_only_v1.json \
    --log-level INFO &> logs/ce_only_retrain.log &
```

**Definition of Done**: CE-only run starts, Stage 1 training begins.

---

#### E4 — Monitor and alert
Set up simple cron check every 30 min:
```bash
*/30 * * * * python3 /home/savitasajwan03/option_trading/monitor.py \
    --campaign velocity_screen_campaign_v1 \
    >> /home/savitasajwan03/logs/monitor.log 2>&1
```

Alert conditions:
- Any lane `status: error` → ping team
- All lanes complete → ping team for D4 signal check
- CE-only run `stage2_cv_gate_failed` → expected (bypass), don't alert

**Definition of Done**: Cron job registered, `crontab -l` shows entry.

---

## Dependency Graph

```
IMMEDIATELY (TODAY):
  B1 — Add vel_ to feature sets
  B2 — ADX + vol spike + gap features
  C1 — bypass_stage2 flag in pipeline.py
  C2 — velocity_hpo_v1 manifest
  C3 — ce_only manifest

AFTER B1 + C2 DONE:
  D1 — velocity_screen_campaign_v1 config
  D3 — ce_only_retrain_v1 config

AFTER TEAM A PROMOTES DATASET + D1 + D3 CONFIGS READY:
  E2 — Launch velocity screen
  E3 — Launch CE-only retrain (parallel)

AFTER SCREEN FINISHES (~48 hrs):
  D4 — Feature signal diagnostic
  D2 — velocity_full_retrain_v1 config (if signal passes)
  E1 — Resource check
  E launch full retrain

AFTER FULL RETRAIN FINISHES (~48 hrs):
  Review results against publish gates:
    profit_factor >= 1.5
    trades >= 50
    max_drawdown <= 10%
    side_share in [0.30, 0.70]
```

---

## Definition of Done — Full Workstream

| Team | Done when |
|------|-----------|
| **A** | v2 validated, promoted to `snapshots_ml_flat`, enrichment VM stopped |
| **B** | vel_ + ADX + gap + vol_spike in feature sets, tests pass |
| **C** | bypass_stage2 works, velocity_hpo_v1 manifest valid, wiring test passes |
| **D** | All 3 campaign configs created and validate, signal check gate documented |
| **E** | Campaigns running, monitor cron active |
| **SYSTEM** | At least 1 model hits: PF ≥ 1.5, trades ≥ 50, drawdown ≤ 10% |

---

## What We Are NOT Doing

```
✗ LSTM / Transformer — overkill for tabular options data
✗ MACD feature — EMA slope already captures it
✗ Bollinger Bands — ATR + IV cover volatility better
✗ Random train/test split — time-based CV only
✗ Reusing old Stage 1 artifact — full retrain with velocity features
✗ Lowering publish gate to 1.3 PF — slippage makes it unviable
✗ Deploying before shadow phase (3 weeks min)
```

---

## Publish Gate (unchanged)

```
profit_factor >= 1.5
trades >= 50
max_drawdown <= 10%
net_return > 0
side_share in [0.30, 0.70]   (not CE-only > 0.80 without special review)

Shadow phase: 3 weeks live data before position sizing up
Week 1-2: 25% size
Week 3-4: 50% if P&L positive
Month 2+: full size if stable
```

---

## Timeline (best case)

```
Day 0 (today):     B + C teams start coding
Day 1:             Enrichment finishes → Team A validates + promotes
Day 2:             B + C complete → D1/D3 configs ready
Day 2 (evening):   E launches velocity screen + CE-only retrain
Day 4:             CE-only result available (2 day run)
Day 4:             Velocity screen ~50% done
Day 5:             Screen complete → D4 signal check → D2 full retrain config
Day 5 (evening):   Full retrain launches
Day 7:             Full retrain complete → review results
Day 7–28:          Shadow deploy (whichever model passes gates)
Day 28:            Shadow review → ship decision
```
