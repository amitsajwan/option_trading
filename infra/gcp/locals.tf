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
