output "runtime_external_ip" {
  value       = google_compute_address.runtime.address
  description = "Static external IP assigned to the runtime VM."
}

output "artifact_registry_repository" {
  value       = google_artifact_registry_repository.runtime.id
  description = "Artifact Registry repository resource ID."
}

output "artifact_registry_image_prefix" {
  value       = "${local.artifact_registry_host}/${var.project_id}/${google_artifact_registry_repository.runtime.repository_id}"
  description = "Base image path prefix used by docker-compose.gcp.yml."
}

output "model_bucket_url" {
  value       = "gs://${google_storage_bucket.models.name}"
  description = "Cloud Storage bucket for published models."
}

output "runtime_config_bucket_url" {
  value       = "gs://${google_storage_bucket.runtime_config.name}"
  description = "Cloud Storage bucket for runtime bootstrap files."
}

output "training_instance_template" {
  value       = google_compute_instance_template.training.self_link_unique
  description = "Instance template self-link for creating disposable training VMs."
}
