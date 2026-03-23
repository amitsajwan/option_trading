# Training Release Runbook

Use this runbook to train, publish, and generate the staged `ml_pure` runtime handoff.

This workflow is self-contained. It includes the GCP setup it needs.

Host note:

- runtime and training execution are container/VM-first
- local Python on Windows is only needed for VS Code features, host-side tests, or running helper scripts directly from the repo

## What This Produces

- one disposable training VM
- a completed staged research run
- published staged model artifacts in the model bucket
- updated runtime config bundle in the runtime-config bucket
- `release/ml_pure_runtime.env`

## Step 1: Prepare Shared GCP Resources

If the shared training/runtime resources do not exist yet, create them first:

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
- `MODEL_BUCKET_URL`
- `RUNTIME_CONFIG_BUCKET_URL`
- `DATA_SYNC_SOURCE`
- `TRAINING_VM_NAME`
- `MODEL_GROUP`
- `PROFILE_ID`
- `STAGED_CONFIG`
- `RAW_ARCHIVE_BUCKET_URL`
- `SNAPSHOT_PARQUET_BUCKET_URL`

`DATA_SYNC_SOURCE` should point at the parent prefix that syncs into `.data/ml_pipeline` on the VM and already contains `parquet_data/` underneath it. After startup, the expected local path is `${REPO_ROOT}/.data/ml_pipeline/parquet_data` when `REPO_ROOT` is set in `ops/gcp/operator.env`, otherwise `/opt/option_trading/.data/ml_pipeline/parquet_data`.

The one-command snapshot flow must already have published the following before training starts:

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

Verify:

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

If those datasets are missing, stop here and complete the snapshot workflow first.

For long-running staged training, always use `tmux`.
If the SSH session drops while the training job is running in a plain foreground shell, the training process usually stops with the shell.

Basic `tmux` commands:

```bash
tmux new -s training
tmux attach -t training
tmux ls
```

Detach from `tmux` without stopping the run:

- press `Ctrl+b`
- then press `d`

## Step 4: Run The Staged Release Pipeline

On the training VM:

```bash
cd /opt/option_trading
tmux new -s training
```

Inside the `tmux` session:

```bash
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release.log
```

Research-only variants:

Use these when the default manifest holds too early and you want more evidence before changing the production manifest.
Both commands disable runtime handoff and runtime-config publish, and they write into separate model groups.

Stage 1 HPO search:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-hpo.log
```

Deeper Stage 1 search:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_deep" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.deep_search.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-deep.log
```

Stage 2 HPO search:

Use this after a deep-search run clears Stage 1 and then holds on `stage2_cv`.

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_stage2_hpo" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_hpo.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-stage2-hpo.log
```

Stage 2 edge-filter search:

Use this after both `deep_search` and `stage2_hpo` hold at `stage2_cv` with nearly identical metrics.
It tightens the Stage 2 label set before the signal check and Stage 2 CV gate.

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_stage2_edge" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage2_edge_filter.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-stage2-edge.log
```

Stage 1 diagnostic gates:

```bash
APPLY_RUNTIME_HANDOFF=0 \
PUBLISH_RUNTIME_CONFIG=0 \
MODEL_GROUP="banknifty_futures/h15_tp_auto_diag" \
STAGED_CONFIG="ml_pipeline_2/configs/research/staged_dual_recipe.stage1_diagnostic.json" \
bash ./ops/gcp/run_staged_release_pipeline.sh 2>&1 | tee training-release-diag.log
```

Training Grid V1:

Use this when you want one deterministic research sweep across the baseline, three Stage 2 edge thresholds, the best-threshold expiry-block lane, and the best-threshold time-focus lane.
The grid runner is research-only by default. It writes generated per-run manifests plus a single `grid_summary.json`, and it does not publish runtime config or hand off a live runtime bundle unless you explicitly rerun the selected winner through the normal release flow later.
The checked-in `prod_v1` grid sets `grid.max_parallel_runs=2`. With the current deep-search base manifest using `training.runtime.model_n_jobs=8`, that maps cleanly onto a 16-core VM: two independent lanes at a time, eight model threads each.

```bash
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.prod_v1.json \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual
```

Look for:

- a new `ml_pipeline_2/artifacts/research/staged_grid_prod_v1_<timestamp>/grid_summary.json`
- generated per-run manifests under `.../manifests`
- one run directory per lane under `.../runs`
- `execution.max_parallel_runs=2` in `grid_summary.json`
- `stage2_hpo_escalation.eligible=true|false` in `grid_summary.json`
- `winner.grid_run_id` and `winner.publishable` in `grid_summary.json`

To reuse the same grid for another instrument:

1. create or copy an instrument-specific staged base manifest
2. point `inputs.base_manifest_path` in the grid config to that base manifest
3. keep the grid runner command the same, but change `--model-group` to the new instrument namespace

The grid runner is intentionally loosely coupled to instrument choice. Instrument-specific parquet roots, windows, label settings, and per-stage search defaults stay in the base staged manifest. The grid config only layers run-to-run overrides such as Stage 2 edge filters, Stage 2 feature-set variants, and expiry blocking.

Verify:

- command exits successfully
- output includes `Staged release pipeline complete`
- output prints `release status`
- when `release status` is `published`, output also prints the `runtime handoff` path
- follow live progress from another SSH session with `tail -f /opt/option_trading/training-release.log`
- if you disconnect, reconnect and run `tmux attach -t training`

The staged release wrapper is HOLD-safe. If the research run fails a gate, it still writes `summary.json`, `release/assessment.json`, and `release/release_summary.json` with `release_status: held`. In that case, do not expect `release/ml_pure_runtime.env` or runtime-config publish output. Inspect the blocking reasons and rerun only after fixing the upstream issue.

Recommended research order after the default manifest holds:

1. Run the deep-search manifest first. It tells you whether Stage 1 or Stage 2 is actually the bottleneck.
2. If `completion_mode=stage1_cv_gate_failed`, run the Stage 1 HPO manifest.
3. If `completion_mode=stage2_cv_gate_failed`, try the Stage 2 HPO manifest once.
4. If Stage 2 still holds with nearly unchanged metrics, run the Stage 2 edge-filter manifest next.
5. If Stage 1 still only narrowly fails after the search runs, use the diagnostic manifest to measure gate sensitivity.
6. Only after those runs decide whether to change the default manifest or the underlying feature/label design.

Also verify the latest local release payload:

```bash
ls -lh /opt/option_trading/training-release.json
```

For `PUBLISH`, also verify the latest local release handoff:

```bash
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/ml_pure_runtime.env" | sort | tail -n 1
```

Look for:

- a concrete `release/ml_pure_runtime.env` path

If you were disconnected and are not sure whether the job finished:

```bash
cd /opt/option_trading
pgrep -af "run_staged_release_pipeline.sh|ml_pipeline_2.run_staged_release"
tail -n 50 training-release.log
find /opt/option_trading/ml_pipeline_2/artifacts/research -path "*/release/ml_pure_runtime.env" | sort | tail -n 1
```

Interpretation:

- active process found: training is still running; reattach with `tmux attach -t training`
- no active process and `Staged release pipeline complete` is present: proceed to publish verification
- no active process and the wrapper exited without the completion marker: do not assume interruption only; inspect the latest staged artifacts first
- no active process and `training-release.json` exists with `release_status: held`: the wrapper completed correctly and rejected publish

When the wrapper exits without the completion marker, inspect the newest staged run directory before rerunning anything:

```bash
cd /opt/option_trading
LATEST_RUN="$(ls -1dt ml_pipeline_2/artifacts/research/* 2>/dev/null | head -n 1)"
echo "${LATEST_RUN}"
find "${LATEST_RUN}" -maxdepth 2 \( -name summary.json -o -name assessment.json -o -name release_summary.json \) | sort
```

Interpret those artifacts before deciding what to do:

- `summary.json` exists and `publish_assessment.decision` is `HOLD`: the staged run completed enough to reject publish; investigate blocking reasons instead of rerunning blindly
- `release/assessment.json` exists but `release/ml_pure_runtime.env` does not: the release step reached publish assessment and rejected the candidate as non-publishable
- no staged summary artifacts exist for the latest run: the wrapper likely stopped before the research run completed; rerun the wrapper from the top

## Step 5: Verify Publish Results

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

- the published model group under the model bucket
- runtime config bundle files under the runtime-config bucket

What to inspect in the run summary:

- `publish_assessment.decision` should be `PUBLISH`
- `publish_assessment.publishable` should be `true`
- `blocking_reasons` should be empty

Useful artifact meanings:

- `summary.json`: staged research summary, including `publish_assessment`, stage artifacts, CV prechecks, and any early-hold outcome
- `release/assessment.json`: publishability decision for the completed staged run
- `release/release_summary.json`: final publish and handoff result for the completed staged release; this exists for both `PUBLISH` and `HOLD` outcomes
- `release/ml_pure_runtime.env`: runtime handoff for deployment; this exists only after a successful publish

If the staged release returns `HOLD`, stop and investigate the holdout gates before live deployment.

## Step 6: Delete Temporary Training Infra

Delete the disposable training VM after publish is complete:

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
- Artifact Registry
- runtime VM
