# IMPORTANT: This configuration uses local Terraform state by default.
# Local state means infra can only be recreated from the machine that last
# ran `terraform apply`. For team use or recovery from any machine, enable
# the GCS backend below and run `terraform init -migrate-state` once:
#
# terraform {
#   backend "gcs" {
#     bucket = "<your-project>-option-trading-runtime-config"
#     prefix = "terraform/state"
#   }
# }
#
# The runtime-config bucket already exists after first bootstrap, so this
# costs nothing extra. Without it, losing the local .tfstate means you must
# recreate the VM manually via `gcloud compute instances create` (see
# docs/runbooks/LIVE_SETUP_GUIDE.md Phase 12.2).

terraform {
  required_version = ">= 1.6.0"

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
