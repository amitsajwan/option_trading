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
