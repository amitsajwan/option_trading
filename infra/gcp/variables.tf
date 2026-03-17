variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Primary GCP region."
  type        = string
}

variable "zone" {
  description = "Primary GCP zone."
  type        = string
}

variable "network" {
  description = "VPC network name."
  type        = string
  default     = "default"
}

variable "subnetwork" {
  description = "Optional subnetwork self-link or name."
  type        = string
  default     = ""
}

variable "runtime_name" {
  description = "Runtime VM name."
  type        = string
  default     = "option-trading-runtime"
}

variable "runtime_machine_type" {
  description = "Machine type for always-on runtime VM."
  type        = string
  default     = "n2-standard-8"
}

variable "runtime_boot_disk_gb" {
  description = "Boot disk size for runtime VM."
  type        = number
  default     = 100
}

variable "runtime_os_image" {
  description = "Boot image for runtime VM."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}

variable "training_template_name" {
  description = "Training VM instance template name."
  type        = string
  default     = "option-trading-training-template"
}

variable "training_machine_type" {
  description = "Machine type for disposable training VM."
  type        = string
  default     = "n2-highmem-32"
}

variable "training_boot_disk_gb" {
  description = "Boot disk size for training VM template."
  type        = number
  default     = 250
}

variable "training_os_image" {
  description = "Boot image for training VM template."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}

variable "runtime_tag" {
  description = "Network tag for runtime VM."
  type        = string
  default     = "option-trading-runtime"
}

variable "training_tag" {
  description = "Network tag for training VM."
  type        = string
  default     = "option-trading-training"
}

variable "ssh_source_ranges" {
  description = "CIDR ranges allowed to SSH to the VMs."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "dashboard_source_ranges" {
  description = "CIDR ranges allowed to reach the dashboard port."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "dashboard_port" {
  description = "External dashboard port."
  type        = number
  default     = 8008
}

variable "artifact_registry_repository_id" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "option-trading-runtime"
}

variable "artifact_registry_location" {
  description = "Artifact Registry location."
  type        = string
  default     = "asia-south1"
}

variable "artifact_registry_host" {
  description = "Artifact Registry hostname, for example asia-south1-docker.pkg.dev."
  type        = string
  default     = ""
}

variable "model_bucket_name" {
  description = "Cloud Storage bucket for published models."
  type        = string
}

variable "runtime_config_bucket_name" {
  description = "Cloud Storage bucket for runtime bootstrap files."
  type        = string
}

variable "create_snapshot_data_bucket" {
  description = "Whether Terraform should create a bucket for raw archive and final historical parquet artifacts."
  type        = bool
  default     = false
}

variable "snapshot_data_bucket_name" {
  description = "Optional Cloud Storage bucket for raw BankNifty archive and final historical parquet outputs."
  type        = string
  default     = ""
}

variable "create_training_data_bucket" {
  description = "Whether Terraform should create a training data bucket."
  type        = bool
  default     = false
}

variable "training_data_bucket_name" {
  description = "Optional Cloud Storage bucket for frozen ML inputs."
  type        = string
  default     = ""
}

variable "repo_clone_url" {
  description = "Git clone URL for the option_trading repo."
  type        = string
}

variable "repo_ref" {
  description = "Git branch, tag, or commit to check out on boot."
  type        = string
  default     = "main"
}

variable "app_image_tag" {
  description = "Container image tag used by docker-compose.gcp.yml."
  type        = string
  default     = "latest"
}

variable "runtime_config_sync_source" {
  description = "gs:// prefix containing .env.compose and optional credentials bundle."
  type        = string
  default     = ""
}

variable "published_models_sync_source" {
  description = "gs:// prefix containing published model artifacts."
  type        = string
  default     = ""
}

variable "data_sync_source" {
  description = "gs:// prefix containing .data/ml_pipeline contents."
  type        = string
  default     = ""
}

variable "enable_dashboard_profile" {
  description = "Whether the runtime bootstrap should also start the dashboard profile."
  type        = bool
  default     = true
}

variable "runtime_service_account_id" {
  description = "Service account ID for the runtime VM."
  type        = string
  default     = "option-trading-runtime"
}

variable "training_service_account_id" {
  description = "Service account ID for the training VM."
  type        = string
  default     = "option-trading-training"
}

variable "runtime_os_user" {
  description = "Primary OS user that should be added to the docker group."
  type        = string
  default     = "ubuntu"
}
