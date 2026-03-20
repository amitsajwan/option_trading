# Runbooks Index

Start here if you are acting as release manager or operator.

There are three main workflow docs:

1. [GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md](GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md)
   Use this for historical snapshot and parquet creation.
2. [TRAINING_RELEASE_RUNBOOK.md](TRAINING_RELEASE_RUNBOOK.md)
   Use this for staged ML training, publish, and runtime handoff generation.
3. [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md)
   Use this for live runtime image build, config publish, container startup, validation, and rollback.

Each workflow doc is self-contained:

- GCP setup needed for that workflow is included inside the same file
- every step has a `Verify:` section
- every verification block says what to look for before moving on

Supporting runbook:

4. [CLEANUP_ROLLBACK_RUNBOOK.md](CLEANUP_ROLLBACK_RUNBOOK.md)
   Use this to stop spend, remove temporary compute, or roll back runtime config.

Read [../SYSTEM_SOURCE_OF_TRUTH.md](../SYSTEM_SOURCE_OF_TRUTH.md) first if you need the non-negotiable current runtime and training rules.
