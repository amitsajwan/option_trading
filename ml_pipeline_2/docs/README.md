# ml_pipeline_2 Docs

This directory contains the maintained module documentation for `ml_pipeline_2`.

Use these files as the source of truth:

- [architecture.md](architecture.md)
  - package boundary, supported staged flow, artifact model, and design rules
- [detailed_design.md](detailed_design.md)
  - file-by-file design map for `src/ml_pipeline_2`
- [gcp_user_guide.md](gcp_user_guide.md)
  - supported operator flow for staged training, release, and publish handoff
- [ubuntu_gcp_runbook.md](ubuntu_gcp_runbook.md)
  - Ubuntu/GCP execution notes for this module

Supported staged manifest:

- [`../configs/research/staged_dual_recipe.default.json`](../configs/research/staged_dual_recipe.default.json)

Repo-level documents that remain outside this module because they are cross-system:

- [`../../docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](../../docs/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [`../../docs/GCP_BOOTSTRAP_RUNBOOK.md`](../../docs/GCP_BOOTSTRAP_RUNBOOK.md)
- [`../../docs/GCP_DEPLOYMENT.md`](../../docs/GCP_DEPLOYMENT.md)
- [`../../docs/FROM_SCRATCH_OPERATOR_GUIDE.md`](../../docs/FROM_SCRATCH_OPERATOR_GUIDE.md)

Do not add maintained architecture or design docs at the `ml_pipeline_2` module root.
