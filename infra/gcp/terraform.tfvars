project_id                   = "amittrading-493606"
region                       = "asia-south1"
zone                         = "asia-south1-b"
repo_clone_url               = "https://github.com/amitsajwan/option_trading.git"
repo_ref                     = "main"

runtime_name                 = "option-trading-runtime-01"
runtime_machine_type         = "e2-standard-4"
runtime_boot_disk_gb         = 100
training_machine_type        = "n2-highmem-8"
training_boot_disk_gb        = 250

artifact_registry_repository_id = "option-trading-runtime"
artifact_registry_location      = "asia-south1"
app_image_tag                   = "latest"

model_bucket_name            = "amittrading-493606-option-trading-models"
runtime_config_bucket_name   = "amittrading-493606-option-trading-runtime-config"
create_snapshot_data_bucket  = false
snapshot_data_bucket_name    = "amittrading-493606-option-trading-snapshots"

runtime_config_sync_source   = "gs://amittrading-493606-option-trading-runtime-config/runtime"
published_models_sync_source = "gs://amittrading-493606-option-trading-models/published_models"
data_sync_source             = "gs://amittrading-493606-option-trading-snapshots/ml_pipeline"

dashboard_port               = 8008
enable_dashboard_profile     = true

ssh_source_ranges            = ["0.0.0.0/0"]
dashboard_source_ranges      = ["0.0.0.0/0"]
