resource "google_storage_bucket" "models" {
  name                        = var.model_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }
}

resource "google_storage_bucket" "runtime_config" {
  name                        = var.runtime_config_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "snapshot_data" {
  count                       = var.create_snapshot_data_bucket ? 1 : 0
  name                        = var.snapshot_data_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "training_data" {
  count                       = var.create_training_data_bucket ? 1 : 0
  name                        = var.training_data_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
}
