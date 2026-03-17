locals {
  artifact_registry_host = trimspace(var.artifact_registry_host) != "" ? trimspace(var.artifact_registry_host) : "${var.artifact_registry_location}-docker.pkg.dev"

  runtime_project_roles = [
    "roles/artifactregistry.reader",
    "roles/storage.objectViewer",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]

  training_project_roles = [
    "roles/storage.objectViewer",
    "roles/storage.objectAdmin",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]
}

resource "google_artifact_registry_repository" "runtime" {
  location      = var.artifact_registry_location
  repository_id = var.artifact_registry_repository_id
  description   = "Runtime Docker images for option_trading."
  format        = "DOCKER"
}

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

resource "google_storage_bucket" "training_data" {
  count                       = var.create_training_data_bucket ? 1 : 0
  name                        = var.training_data_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
}

resource "google_service_account" "runtime" {
  account_id   = var.runtime_service_account_id
  display_name = "Option Trading Runtime"
}

resource "google_service_account" "training" {
  account_id   = var.training_service_account_id
  display_name = "Option Trading Training"
}

resource "google_project_iam_member" "runtime_roles" {
  for_each = toset(local.runtime_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "training_roles" {
  for_each = toset(local.training_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.training.email}"
}

resource "google_compute_address" "runtime" {
  name   = "${var.runtime_name}-ip"
  region = var.region
}

resource "google_compute_firewall" "runtime_ssh" {
  name    = "${var.runtime_name}-allow-ssh"
  network = var.network

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.ssh_source_ranges
  target_tags   = [var.runtime_tag, var.training_tag]
}

resource "google_compute_firewall" "runtime_dashboard" {
  name    = "${var.runtime_name}-allow-dashboard"
  network = var.network

  allow {
    protocol = "tcp"
    ports    = [tostring(var.dashboard_port)]
  }

  source_ranges = var.dashboard_source_ranges
  target_tags   = [var.runtime_tag]
}

resource "google_compute_instance" "runtime" {
  name         = var.runtime_name
  machine_type = var.runtime_machine_type
  zone         = var.zone
  tags         = [var.runtime_tag]

  boot_disk {
    initialize_params {
      image = var.runtime_os_image
      size  = var.runtime_boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network    = var.network
    subnetwork = trimspace(var.subnetwork) != "" ? var.subnetwork : null

    access_config {
      nat_ip = google_compute_address.runtime.address
    }
  }

  service_account {
    email  = google_service_account.runtime.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile("${path.module}/templates/runtime-startup.sh.tftpl", {
    artifact_registry_host        = local.artifact_registry_host
    artifact_registry_repository  = google_artifact_registry_repository.runtime.repository_id
    app_image_tag                 = var.app_image_tag
    dashboard_port                = var.dashboard_port
    enable_dashboard_profile      = var.enable_dashboard_profile
    runtime_config_sync_source    = var.runtime_config_sync_source
    published_models_sync_source  = var.published_models_sync_source
    data_sync_source              = var.data_sync_source
    project_id                    = var.project_id
    repo_clone_url                = var.repo_clone_url
    repo_ref                      = var.repo_ref
    runtime_os_user               = var.runtime_os_user
  })
}

resource "google_compute_instance_template" "training" {
  name_prefix  = "${var.training_template_name}-"
  machine_type = var.training_machine_type
  tags         = [var.training_tag]

  disk {
    auto_delete  = true
    boot         = true
    source_image = var.training_os_image
    disk_size_gb = var.training_boot_disk_gb
    disk_type    = "pd-balanced"
  }

  network_interface {
    network    = var.network
    subnetwork = trimspace(var.subnetwork) != "" ? var.subnetwork : null
    access_config {}
  }

  service_account {
    email  = google_service_account.training.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = templatefile("${path.module}/templates/training-startup.sh.tftpl", {
    data_sync_source = var.data_sync_source
    project_id       = var.project_id
    repo_clone_url   = var.repo_clone_url
    repo_ref         = var.repo_ref
    runtime_os_user  = var.runtime_os_user
  })
}
