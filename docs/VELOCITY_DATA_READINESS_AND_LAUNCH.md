# Velocity Data Readiness And Launch
> **Status**: Active  
> **Last updated**: 2026-04-12  
> **Purpose**: Source of truth for when a velocity campaign is allowed to run.

---

## 1. Why The Previous Velocity Run Was Invalid

The failed `velocity_screen_campaign_v1` run exposed a bad boundary:

- `support_dataset = snapshots_ml_flat_v2`
- staged manifests still pointed at `stage1_entry_view_v1`, `stage2_direction_view_v1`, `stage3_recipe_view_v1`
- the `v1` stage views were built from the old flat dataset, not from `snapshots_ml_flat_v2`

That creates two distinct failure modes:

1. **Key mismatch**
   - stage views and support dataset can carry different `trade_date/timestamp/snapshot_id` sets
   - this caused `stage frame/label join mismatch`

2. **Feature under-wiring**
   - velocity campaigns can request `fo_velocity_v1`
   - but the stage views only expose what was projected into them
   - if the views are still `v1`, the enriched velocity fields are not guaranteed to be present

The fix is not to patch around the join.  
The fix is to make the support dataset, stage views, and manifests all use the same versioned source.

---

## 2. Required Data Contract

### Baseline path
- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`
- `stage*_view_id = *_v1`

Keep this untouched as the stable fallback.

### Velocity-ready path
- `snapshots_ml_flat_v2`
- `stage1_entry_view_v2`
- `stage2_direction_view_v2`
- `stage3_recipe_view_v2`
- `stage*_view_id = *_v2`

Velocity manifests must use the full `v2` path end-to-end.

---

## 3. Gate 0: Snapshot Readiness

Do **not** launch any velocity campaign until all of the following are true:

1. `snapshots_ml_flat_v2` exists for the requested window
2. `snapshots_ml_flat_v2` passes schema/readiness checks
3. `stage1_entry_view_v2`, `stage2_direction_view_v2`, `stage3_recipe_view_v2` have been rebuilt from `snapshots_ml_flat_v2`
4. support dataset and stage views match on:
   - `trade_date`
   - `timestamp`
   - `snapshot_id`
5. requested feature families resolve to real columns in the stage views
6. `fo_velocity_v1` resolves to actual velocity/enrichment columns, not just fallback anchors
7. a one-lane smoke run completes Stage 1/2 setup without join mismatch

If any one of those fails, velocity campaign launch is blocked.

---

## 4. GCP Sequence

Use the same GCP machine:
- `option-trading-snapshot-build-01`

### Step A — Freeze incorrect training
If `velocity_screen_campaign_v1` or its spawned `run_staged_grid` workers are still active, stop them.

### Step B — Validate the support dataset
Run the staged preflight against the velocity manifest:

```bash
cd ~/option_trading
/home/savitasajwan03/option_trading/.venv/bin/python \
  -m ml_pipeline_2.run_staged_data_preflight \
  --config ml_pipeline_2/configs/research/staged_dual_recipe.velocity_screen_v1.json \
  --output /home/savitasajwan03/logs/velocity_preflight.json
```

This must return `status: "pass"` before launch.

### Step C — Rebuild versioned stage views from the enriched flat dataset

```bash
cd ~/option_trading
/home/savitasajwan03/option_trading/.venv/bin/python \
  -m snapshot_app.historical.rebuild_stage_views_from_flat \
  --parquet-root /home/savitasajwan03/option_trading/.data/ml_pipeline/parquet_data \
  --source-flat-dataset snapshots_ml_flat_v2 \
  --output-stage1-dataset stage1_entry_view_v2 \
  --output-stage2-dataset stage2_direction_view_v2 \
  --output-stage3-dataset stage3_recipe_view_v2
```

Then rerun preflight.

### Step D — Smoke before full screen
Run a single-lane or single-manifest smoke using the `v2` views.

Only if smoke passes:
- relaunch the full velocity screen campaign

---

## 5. Manifest Rules

Velocity manifests must use:

- `support_dataset = snapshots_ml_flat_v2`
- `stage1_view_id = stage1_entry_view_v2`
- `stage2_view_id = stage2_direction_view_v2`
- `stage3_view_id = stage3_recipe_view_v2`

That is now the required launch contract for:

- `staged_dual_recipe.velocity_screen_v1.json`
- `staged_dual_recipe.velocity_hpo_v1.json`

---

## 6. What The Preflight Must Catch

The preflight gate is there to fail fast on:

- missing `snapshots_ml_flat_v2`
- missing `*_view_v2`
- support/view key mismatch
- feature sets resolving to zero columns
- `fo_velocity_v1` resolving without real velocity columns
- staged views missing planned enrichment columns

This is intentionally stricter than the training pipeline.  
The point is to fail in seconds, not after 30-40 minutes of wasted compute.

---

## 7. Relaunch Rule

`velocity_screen_campaign_v1` may be relaunched only after:

1. preflight passes
2. smoke passes
3. no duplicate old campaign processes remain

Until then, the correct status is:

```text
velocity launch blocked by Gate 0
```
