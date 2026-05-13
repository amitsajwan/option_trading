# Terraform state is stored in the runtime-config GCS bucket.
# This allows infra to be managed from any machine that has gcloud auth.
#
# BOOTSTRAP NOTE: On a brand-new project the GCS bucket does not exist yet.
# from_scratch_bootstrap.sh handles this automatically:
#   1. First apply uses a local backend (no bucket needed)
#   2. After Terraform creates the bucket, state is migrated to GCS
# You do not need to manually edit this file.

terraform {
  required_version = ">= 1.6.0"

  backend "local" {}
  # After first apply, from_scratch_bootstrap.sh migrates state to GCS:

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
