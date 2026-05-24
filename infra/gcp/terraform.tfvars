project_id                   = "algo-trading-496203"
region                       = "asia-south1"
zone                         = "asia-south1-b"
repo_clone_url               = "https://github.com/amitsajwan/option_trading.git"
repo_ref                     = "main"

runtime_name                 = "option-trading-runtime-01"
# Unified recommendation: e2-highmem-16 (16 vCPU, 128 GB) — see docs/GCP_UNIFIED_VM.md
runtime_machine_type         = "e2-highmem-16"
training_machine_type        = "e2-highmem-16"

artifact_registry_repository_id = "option-trading-runtime"
artifact_registry_location      = "asia-south1"
app_image_tag                   = "latest"

model_bucket_name            = "algo-trading-496203-option-trading-models"
runtime_config_bucket_name   = "algo-trading-496203-option-trading-runtime-config"

runtime_config_sync_source   = "gs://algo-trading-496203-option-trading-runtime-config/runtime"
published_models_sync_source = "gs://algo-trading-496203-option-trading-models/published_models"
data_sync_source             = "gs://algo-trading-496203-option-trading-snapshots/ml_pipeline"

dashboard_port               = 8008
enable_dashboard_profile     = true

ssh_source_ranges            = ["0.0.0.0/0"]
dashboard_source_ranges      = ["0.0.0.0/0"]
create_snapshot_data_bucket  = true
snapshot_data_bucket_name    = "algo-trading-496203-option-trading-snapshots"
