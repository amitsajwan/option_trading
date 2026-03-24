# Cleanup And Rollback Runbook

Use this when you want to stop spend, delete disposable compute, or roll the live runtime back to a known-good state.

## Audience

- release manager
- GCP operator
- on-call operator handling end-of-day cleanup

## Cleanup Modes

### Cheap Idle State

Use this when you want to stop compute cost but keep the runtime VM definition and Terraform-managed infra.

Commands:

```bash
./ops/gcp/delete_training_vm.sh
./ops/gcp/stop_runtime.sh
```

Preserved:

- GHCR-published runtime images
- model bucket
- runtime config bucket
- runtime VM definition
- static IP
- firewall rules

### Preserve-Data Teardown

Use this when you want to remove most cost-bearing infra but keep deployable state in GCS.

Command:

```bash
AUTO_APPROVE=1 ./ops/gcp/destroy_infra_preserve_data.sh
```

Preserved:

- model bucket
- runtime config bucket
- optional snapshot data bucket
- any GHCR images already published outside GCP

Destroyed:

- runtime VM
- training instance template
- runtime static IP
- firewall rules
- runtime and training service accounts
- Terraform-managed IAM grants

### Full Wipe

Use this only if you explicitly want everything Terraform manages removed.

Command:

```bash
cd infra/gcp
terraform destroy
```

Warning:

- bucket deletion can fail if buckets are not empty
- full wipe removes deployable state unless you have another backup

## Runtime Rollback

If a new runtime handoff or config publish is bad, roll back by restoring the previous `.env.compose` or previous runtime handoff, republishing runtime config, then restarting the runtime VM.

Typical sequence:

```bash
./ops/gcp/publish_runtime_config.sh
gcloud compute instances stop "${RUNTIME_NAME}" --zone "${ZONE}"
gcloud compute instances start "${RUNTIME_NAME}" --zone "${ZONE}"
```

If the problem is image-related, redeploy the known-good GHCR image tag and repeat the restart.

## Resume Later

After preserve-data teardown, recreate compute with:

```bash
export PATH="$HOME/bin:$PATH"
RUN_IMAGE_BUILD=0 RUN_RUNTIME_CONFIG_SYNC=0 ./ops/gcp/from_scratch_bootstrap.sh
```

Rebuild images only if code changed. Republish runtime config only if `.env.compose` changed.

## Validation

Cheap idle state:

```bash
gcloud compute instances describe "${RUNTIME_NAME}" --zone "${ZONE}" --format="value(status)"
```

Preserve-data teardown:

```bash
gcloud storage ls "${MODEL_BUCKET_URL}"
gcloud storage ls "${RUNTIME_CONFIG_BUCKET_URL}"
```

## Related Files

- [ops/gcp/stop_runtime.sh](../../ops/gcp/stop_runtime.sh)
- [ops/gcp/delete_training_vm.sh](../../ops/gcp/delete_training_vm.sh)
- [ops/gcp/destroy_infra_preserve_data.sh](../../ops/gcp/destroy_infra_preserve_data.sh)
- [README.md](README.md)
