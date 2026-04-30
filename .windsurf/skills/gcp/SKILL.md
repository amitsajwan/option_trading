---
name: gcp
description: Connect to GCP VM instances via SSH, run commands, and manage ML workloads on remote machines
---

# GCP Remote Execution Skill

This skill allows the agent to:
- SSH into GCP VM instances
- Execute commands
- Run ML jobs
- Switch to the correct Linux user for ML workloads

---

## 🔐 SSH Access

Use standard SSH:

```bash
gcloud compute ssh <INSTANCE_NAME> --zone=asia-south1-b