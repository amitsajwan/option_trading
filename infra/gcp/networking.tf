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
