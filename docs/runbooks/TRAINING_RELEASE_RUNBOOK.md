# Training Release Runbook

Use this runbook to train staged ML candidates, run research lanes without collisions, publish a winner, and generate the `ml_pure` runtime handoff.

This workflow is self-contained. It includes the GCP setup it needs.

Host note:

- runtime and training execution are container and VM first
- local Python on Windows is only needed for editor features, host-side tests, or helper scripts

Fresh-project rule:

- do not start training until the snapshot/parquet runbook has completed successfully
- on a rebuilt environment, use one smoke publish to validate the lane before longer production research

## Fast Path (Interactive)

Use the supported launcher:

```bash
bash ./ops/gcp/start_training_interactive.sh
```

You can also enter through the shared lifecycle menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# choose action 6
```

Session safety:

- when launched from a plain SSH shell, the launcher auto-starts inside a new `tmux` session and exits
- reconnect with the printed command, for example `tmux attach -t training_20260324_123000`

Supported modes:

- full publish
- quick test
- stage1 HPO
- deep search
- stage2 HPO
- stage2 edge filter
- stage1 diagnostic
- grid prod v1

Path pattern for every launched run:

- `ml_pipeline_2/artifacts/training_launches/<utc_stamp>_<nonce>_<mode>_<lane_tag>_<model_group>_<profile_id>/`
  - `training.log`
  - `run/` for the actual staged or grid run output root
  - `training-release.json` for non-grid modes

Parallel safety:

- the launcher asks for `lane_tag` and uses it in run folder naming
- non-publish modes automatically publish to `base_model_group_<lane_tag>` so concurrent research runs do not collide
- publish mode defaults to the base model group, but you can opt into `base_model_group_<lane_tag>`

Decision rule:

- `publish_full` is the production promotion path
- all other modes are research lanes
- when the baseline run holds, choose the next research mode based on the blocked stage instead of rerunning the same config blindly

## What This Produces

- one disposable training VM
- a completed staged research run
- for `PUBLISH`, published model artifacts in the model bucket
- for `PUBLISH`, updated runtime config bundle in the runtime-config bucket
- for `PUBLISH`, `release/ml_pure_runtime.env`
- for `PUBLISH`, `release/runtime_release_manifest.json`
- for `PUBLISH`, refreshed current approved release artifacts under `.run/gcp_release/`
- for `HOLD`, `summary.json`, `release/assessment.json`, and `release/release_summary.json`
- for unexpected failures, terminal failure artifacts under the run root so the operator can inspect the error without guessing the path

## Step 1: Prepare Shared GCP Resources

If the shared training and runtime resources do not exist yet, create them first:

```bash
cp ops/gcp/operator.env.example ops/gcp/operator.env
RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

You need at least these values in `ops/gcp/operator.env`:

- `PROJECT_ID`
- `REGION`
- `ZONE`
- `REPO_CLONE_URL`
- `REPO_REF`
- `MODEL_BUCKET_NAME`
- `RUNTIME_CONFIG_BUCKET_NAME`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`

Current bootstrap derives:

- `MODEL_BUCKET_URL=gs://<MODEL_BUCKET_NAME>/published_models`
- `RUNTIME_CONFIG_BUCKET_URL=gs://<RUNTIME_CONFIG_BUCKET_NAME>/runtime`

`DATA_SYNC_SOURCE` is optional, but when it is used it should point at the parent prefix that syncs into `.data/ml_pipeline` on the VM and already contains `parquet_data/` underneath it.

After startup, the expected local path is `${REPO_ROOT}/.data/ml_pipeline/parquet_data` when `REPO_ROOT` is set in `ops/gcp/operator.env`, otherwise `/opt/option_trading/.data/ml_pipeline/parquet_data`.

The snapshot flow must already have published these datasets before training starts:

- `snapshots_ml_flat`
- `stage1_entry_view`
- `stage2_direction_view`
- `stage3_recipe_view`

Recommended rebuild order around training:

1. bootstrap infra
2. rebuild and publish parquet
3. run one smoke training publish
4. validate with historical replay
5. only then run production research lanes

The training VM should also have these OS packages available:

- `git`
- `python3-venv`
- `tmux`
- `libgomp1`

Verify:

```bash
cd infra/gcp
terraform output
gcloud storage ls "gs://${MODEL_BUCKET_NAME}"
gcloud storage ls "gs://${RUNTIME_CONFIG_BUCKET_NAME}"
```

Look for:

- Terraform outputs succeed
- both buckets exist
- the training instance template exists in Terraform output

## Step 2: Create The Disposable Training VM

```bash
./ops/gcp/create_training_vm.sh
```

Verify:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- `RUNNING`

## Step 3: Verify VM Startup Sync

SSH to the VM:

```bash
gcloud compute ssh "${TRAINING_VM_NAME}" --zone "${ZONE}"
```

On the VM:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv tmux libgomp1
cd /opt/option_trading
git rev-parse --short HEAD
find .data/ml_pipeline/parquet_data -maxdepth 2 -type d | sort
```

Look for:

- repo checkout exists under `/opt/option_trading`
- parquet datasets are present locally
- at minimum:
  - `snapshots_ml_flat`
  - `stage1_entry_view`
  - `stage2_direction_view`
  - `stage3_recipe_view`

Before training, verify the Stage 2 schema guard locally. If the required columns are missing, stop and rerun the snapshot workflow first.

```bash
python - <<'PY'
from pathlib import Path
import pandas as pd

root = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/stage2_direction_view")
sample = next(root.rglob("*.parquet"))
required = [
    "pcr_change_5m",
    "pcr_change_15m",
    "atm_oi_ratio",
    "near_atm_oi_ratio",
    "atm_ce_oi",
    "atm_pe_oi",
]
df = pd.read_parquet(sample, columns=required)
print(sample)
print(df.notna().mean().to_string())
PY
```

For long-running training, always use `tmux`.

Basic `tmux` commands:

```bash
tmux new -s training
tmux attach -t training
tmux ls
```

## Step 4: Run Training

Supported operator path:

```bash
bash ./ops/gcp/start_training_interactive.sh
```

This launcher:

- prompts for mode, base model group, profile, config, and lane tag
- writes logs and release payloads under `ml_pipeline_2/artifacts/training_launches/...`
- pins the actual staged or grid run output root to `.../run/` inside that launch folder
- routes research lanes into collision-safe model groups
- uses the same underlying staged release wrapper as the manual commands below

Manual path:

```bash
cd /opt/option_trading
tmux new -s training
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release.log
```

Research-only variants:

- Stage 1 HPO:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-hpo.log
```

- Deep search:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_deep" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.deep_search.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-deep.log
```

- Stage 2 HPO:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_stage2_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-stage2-hpo.log
```

- Stage 2 edge filter:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_stage2_edge" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_edge_filter.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-stage2-edge.log
```

- Stage 1 diagnostic:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_diag" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_diagnostic.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-diag.log
```

- Grid prod v1:

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.prod_v1.json \
  --run-output-root /opt/option_trading/ml_pipeline_2/artifacts/training_launches/grid_prod_manual/run \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

The staged release wrapper is terminal-artifact safe:

- if the run fails a gate, it still writes `summary.json`, `release/assessment.json`, and `release/release_summary.json` with `release_status: held`
- if the research job crashes unexpectedly after the run root is created, it now writes terminal failure artifacts so the operator gets a concrete error path instead of an empty run directory
- in either case, do not expect `release/ml_pure_runtime.env` or runtime-config publish output unless the release was actually published

Recommended research order after the default manifest holds:

1. Run `deep_search` first. It tells you whether Stage 1 or Stage 2 is actually the bottleneck.
2. If the baseline run holds on Stage 1, use `stage1_hpo` or `stage1_diag` based on how close the metrics are.
3. If `deep_search` clears Stage 1 and then holds on Stage 2, run `stage2_hpo`.
4. If `stage2_hpo` still holds with nearly unchanged Stage 2 metrics, run `stage2_edge`.
5. Publish only after one of those lanes becomes clearly publishable.

Practical interpretation:

- baseline `publish_full` holds on Stage 1: go to `deep_search`
- `deep_search` holds on Stage 2: go to `stage2_hpo`
- `stage2_hpo` still holds on Stage 2 with similar metrics: go to `stage2_edge`

## Step 5: Verify Results

Verify:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/training_launches -name training.log | sort | tail -n 3
find /opt/option_trading/ml_pipeline_2/artifacts/training_launches -path "*/run/summary.json" | sort | tail -n 3
find /opt/option_trading/ml_pipeline_2/artifacts/training_launches -path "*/run/release/release_summary.json" | sort | tail -n 3
```

Look for:

- the latest launch folder
- a `run/summary.json`
- a `run/release/release_summary.json` for staged-release modes

If you launched the job from the interactive menu, prefer the `training_launches/.../run` path over hunting through `ml_pipeline_2/artifacts/research`.

If SSH disconnects mid-run:

- reconnect to the VM
- reattach to the tmux session with the printed session name
- if the process is gone, inspect the latest `training_launches/.../training.log`
- for staged-release modes, inspect `training-release.json` first, then `run/summary.json`, `run/release/assessment.json`, and `run/release/release_summary.json`
- for grid mode, inspect `run/grid_summary.json`

Check the buckets:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

Look for:

- for `PUBLISH`, the published model group under the model bucket
- for `PUBLISH`, runtime config bundle files under the runtime-config bucket
- for `HOLD`, no new production publish output is expected

What to inspect in the run summary:

- `publish_assessment.decision`
- `publish_assessment.publishable`
- `blocking_reasons`

Useful artifact meanings:

- `summary.json`: staged research summary, including `publish_assessment`, stage artifacts, CV prechecks, and early-hold outcomes
- `release/assessment.json`: publishability decision for the completed staged run
- `release/release_summary.json`: final publish and handoff result for the completed staged release; this exists for `PUBLISH`, `HOLD`, and terminal staged-release failures after run root creation
- `release/ml_pure_runtime.env`: runtime handoff for deployment; this exists only after a successful publish
- `release/runtime_release_manifest.json`: machine-readable live deploy manifest for the published release
- `grid_summary.json`: top-level grid orchestration result; this exists for successful grid runs and for top-level grid failures after grid root creation
- `.run/gcp_release/current_runtime_release.json`: current approved release cache used by the live interactive deploy flow
- `.run/gcp_release/current_runtime_release_pointer.json`: current approved release pointer metadata

If the staged release returns `HOLD`, stop and investigate the gates before live deployment. A HOLD result is a valid completed run, not a launcher failure.

For grid runs, a top-level orchestration failure now does both:

- writes `grid_summary.json` with `status: failed`
- exits the CLI with a non-zero status so unattended shells can detect the failure

Common warning interpretation:

- `X does not have valid feature names, but LGBMClassifier was fitted with feature names`
  - this is a preprocessing metadata warning
  - the pipeline now preserves pandas output through the standard preprocessing steps so feature-name drift is easier to avoid
  - treat repeated warnings as a code-quality issue to fix, not as an automatic model-quality failure signal
- `RuntimeWarning: invalid value encountered in divide`
  - this usually comes from correlation checks over constant or near-constant slices
  - the Stage 2 signal precheck now skips zero-variance aligned slices before computing correlation
  - if this warning returns, investigate label collapse or feature degeneracy in the affected dataset window instead of relaxing gates blindly

## Force-Deploying a Research Run (HOLD Override)

Use this path when a research run has demonstrable regime-specific edge but fails the
combined hard gates (e.g. `profit_factor < 1.5` due to TRENDING regime drag) and you
want to deploy it while research continues in parallel.

**Current approved research run:** `staged_deep_hpo_c1_base_20260429_040848`
- VOLATILE PF = 1.31 net (real edge, survives 0.06% cost)
- TRENDING PF = 0.31 net (cost destroys thin edge — blocked at runtime by `regime_gate_v1`)
- Combined PF = 0.87 net (combined fails gate because TRENDING is 57% of sessions)
- Deployed with `regime_gate_v1` active — only VOLATILE and SIDEWAYS sessions trade live

**Next candidate:** `staged_deep_hpo_e1_volatile_only` (running)
- Same as C1 but S2 trained only on VOLATILE+SIDEWAYS sessions
- Expected: sharper decision boundary for edge-present regimes
- Replace C1 once E1 completes and shows VOLATILE PF ≥ 1.3

### Pre-conditions

1. Research run is `mode=completed` (not failed)
2. `operator.env` on the training VM has real GCS bucket values:
   ```
   MODEL_BUCKET_URL=gs://amittrading-493606-option-trading-models/published_models
   RUNTIME_CONFIG_BUCKET_URL=gs://amittrading-493606-option-trading-runtime-config/runtime
   ```
3. Runtime guard file exists: `.run/ml_runtime_guard_live.json`

### Step A: Run on the training VM

SSH to the training VM and run the force-deploy script:

```bash
gcloud compute ssh savitasajwan03@option-trading-ml-01 \
  --zone=asia-south1-b --project=amittrading-493606

cd /home/savitasajwan03/option_trading

RUN_DIR=ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848 \
MODEL_GROUP=banknifty_futures/h15_tp_auto \
PROFILE_ID=openfe_v9_dual \
APP_IMAGE_TAG=latest \
MODEL_BUCKET_URL=gs://amittrading-493606-option-trading-models/published_models \
RUNTIME_CONFIG_BUCKET_URL=gs://amittrading-493606-option-trading-runtime-config/runtime \
bash ops/gcp/force_deploy_research_run.sh
```

The script does six steps automatically:
1. Force-publishes the local model bundle (bypasses HOLD gates)
2. Writes `release/ml_pure_runtime.env` for the run
3. Builds a `force_training_release.json` compatible with the manifest tool
4. Writes `.run/gcp_release/current_runtime_release.json` (live deploy pointer)
5. Syncs published bundle to `gs://amittrading-493606-option-trading-models/published_models`
6. Uploads release manifests (`current_runtime_release.json`, `current_ml_pure_runtime.env`) to the runtime-config bucket

> **Note on step 6:** Only the release pointer files are uploaded from the training VM. The full
> runtime config bundle (`.env.compose` + Kite credentials + runtime guard) is published by
> `start_runtime_interactive.sh` on the operator machine, which is the only place those
> files live. The training VM does not and should not have live Kite credentials.

### Step B: Deploy from operator machine

After the script completes on the training VM, run the live deploy from your operator machine:

```bash
bash ./ops/gcp/start_runtime_interactive.sh
```

It auto-loads the manifest from the runtime-config bucket, applies `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP` into `.env.compose`, runs preflight, and starts/restarts the runtime VM.

### Deploying a new research run (replacing C1 with E1)

When E1 completes and metrics are satisfactory:

```bash
# On training VM
RUN_DIR=ml_pipeline_2/artifacts/research/staged_deep_hpo_e1_volatile_only_<TIMESTAMP> \
MODEL_GROUP=banknifty_futures/h15_tp_auto \
PROFILE_ID=openfe_v9_dual \
bash ops/gcp/force_deploy_research_run.sh

# On operator machine
bash ./ops/gcp/start_runtime_interactive.sh
```

The runtime will pick up the new `ML_PURE_RUN_ID` on restart.

### Rollback to C1

If E1 behaves unexpectedly live, roll back by re-running force_deploy for C1 and restarting:

```bash
RUN_DIR=ml_pipeline_2/artifacts/research/staged_deep_hpo_c1_base_20260429_040848 \
MODEL_GROUP=banknifty_futures/h15_tp_auto \
PROFILE_ID=openfe_v9_dual \
bash ops/gcp/force_deploy_research_run.sh
# then restart runtime
bash ./ops/gcp/start_runtime_interactive.sh
```

## Step 6: Delete Temporary Training Infra

Delete the disposable training VM after training is complete:

```bash
./ops/gcp/delete_training_vm.sh
```

Verify:

```bash
gcloud compute instances describe "${TRAINING_VM_NAME}" \
  --project "${PROJECT_ID}" \
  --zone "${ZONE}" \
  --format="value(status)"
```

Look for:

- instance not found

Keep these shared resources if live runtime still needs them:

- model bucket
- runtime-config bucket
- runtime VM
