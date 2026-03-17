resource "google_artifact_registry_repository" "runtime" {
  location      = var.artifact_registry_location
  repository_id = var.artifact_registry_repository_id
  description   = "Runtime Docker images for option_trading."
  format        = "DOCKER"
}
