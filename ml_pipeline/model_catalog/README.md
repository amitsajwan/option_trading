# Model Catalog

This folder is the source of truth for trading model profiles shown in the dashboard.

Each model/profile entry must have its own folder under:

- `ml_pipeline/model_catalog/models/<model_id>/model.json`

Required fields in `model.json`:

- `instance_key`
- `model_package`
- `threshold_report`
- `training_report_path`

Optional fields:

- `profile_key`
- `title`
- `summary`
- `description`
- `recommended`
- `eval_summary_path`
- `training_report_path`

Notes:

- Paths must be repo-relative and point to the standardized feature-first artifact layout:
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/model/model.joblib`
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/model_contract.json`
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/config/profiles/<profile_id>/threshold_report.json`
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/config/profiles/<profile_id>/training_report.json`
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/reports/*` (optional extras, e.g. eval summary)
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/data/*` (optional raw snapshots)
  - `ml_pipeline/artifacts/models/by_features/<feature_profile>/<horizon_ts_group>/FEATURES.md`
- Multiple catalog entries may share one `model_package` and vary only by profile config.
- Dashboard loads models strictly from this catalog (no hardcoded fallback entries).
- Use `ml_pipeline/model_catalog/validate_catalog.ps1` before adding or updating a model.
