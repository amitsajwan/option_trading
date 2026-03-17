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
