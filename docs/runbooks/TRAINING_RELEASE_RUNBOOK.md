# Training Release Runbook

Use this runbook to train staged ML candidates, run research lanes without collisions, publish a winner, and generate the `ml_pure` runtime handoff.

This workflow is self-contained. It includes the GCP setup it needs.

Host note:

- runtime and training execution are container and VM first
- local Python on Windows is only needed for editor features, host-side tests, or helper scripts

## Fast Path (Interactive)

Use the supported launcher:

```bash
bash ./ops/gcp/start_training_interactive.sh
```

You can also enter through the shared lifecycle menu:

```bash
bash ./ops/gcp/runtime_lifecycle_interactive.sh
# choose action 5
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
- for `HOLD`, `summary.json`, `release/assessment.json`, and `release/release_summary.json`

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
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

The staged release wrapper is HOLD-safe. If the run fails a gate, it still writes `summary.json`, `release/assessment.json`, and `release/release_summary.json` with `release_status: held`. In that case, do not expect `release/ml_pure_runtime.env` or runtime-config publish output.

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
find /opt/option_trading/ml_pipeline_2/artifacts/research -name summary.json | sort | tail -n 1
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/release_summary.json" | sort | tail -n 1
```

Look for:

- a `summary.json`
- a `release/release_summary.json`

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
- `release/release_summary.json`: final publish and handoff result for the completed staged release; this exists for both `PUBLISH` and `HOLD`
- `release/ml_pure_runtime.env`: runtime handoff for deployment; this exists only after a successful publish

If the staged release returns `HOLD`, stop and investigate the gates before live deployment. A HOLD result is a valid completed run, not a launcher failure.

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
