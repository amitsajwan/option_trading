# Terraform state is stored in the runtime-config GCS bucket.
# This allows infra to be managed from any machine that has gcloud auth.
# On first use (or after moving from local state), run once:
#   terraform init -migrate-state
# The bucket is created by the bootstrap and always exists.

terraform {
  required_version = ">= 1.6.0"

  backend "gcs" {
    bucket = "amittrading-493606-option-trading-runtime-config"
    prefix = "terraform/state"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
