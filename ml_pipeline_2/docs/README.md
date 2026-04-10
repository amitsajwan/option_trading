# ml_pipeline_2 Docs

This directory contains the maintained module documentation for `ml_pipeline_2`.

Use these files as the source of truth:

- [midday_recovery_handover.md](midday_recovery_handover.md)
  - newcomer-oriented handover for the MIDDAY staged-model recovery track
  - best first document for engineers taking over this workstream
- [architecture.md](architecture.md)
  - package boundary, supported staged flow, artifact model, and design rules
- [detailed_design.md](detailed_design.md)
  - file-by-file design map for `src/ml_pipeline_2`
- [gcp_user_guide.md](gcp_user_guide.md)
  - module-level detail for staged training, release, and publish handoff
- [ubuntu_gcp_runbook.md](ubuntu_gcp_runbook.md)
  - Ubuntu/GCP execution notes for this module

Supported staged manifest:

- [`../configs/research/staged_dual_recipe.default.json`](../configs/research/staged_dual_recipe.default.json)
- [`../configs/research/staged_grid.prod_v1.json`](../configs/research/staged_grid.prod_v1.json)
  - production-oriented research grid for the staged pipeline
  - base-manifest driven, so the same grid runner can be reused for other instruments by swapping `inputs.base_manifest_path`

Repo-level documents that remain outside this module because they are cross-system:

- [`../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`](../../docs/runbooks/TRAINING_RELEASE_RUNBOOK.md)
- [`../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`](../../docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
- [`../../docs/runbooks/GCP_DEPLOYMENT.md`](../../docs/runbooks/GCP_DEPLOYMENT.md)
- [`../../docs/runbooks/README.md`](../../docs/runbooks/README.md)

Do not add maintained architecture or design docs at the `ml_pipeline_2` module root.
