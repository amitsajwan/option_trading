# Bringing the trading stack back

The stack was shut down on **2026-09-29** when the Dhan account was closed and all GCP
resources were handed back. This page is everything needed to bring it back.

## TL;DR

**All GCP resources were deleted on 2026-09-29.** Coming back means rebuilding
from the laptop backup onto a new VM. That is one command, and it was tested end
to end on a brand-new VM before anything was deleted:

```bash
# from the repo root on your laptop (Git Bash), gcloud installed and logged in:
bash ops/rebuild_new_vm.sh --project <gcp-project> --totp path/to/new/.env.totp
```

It takes about 30 minutes: about 18 to upload the 5 GB backup and about 15 for the
restore. **Every instrument comes back halted.** Nothing trades until you remove an
`operator_halt` yourself (see "Going live").

---

## State at closure (2026-09-29)

| What | Where |
|---|---|
| Code | GitHub `amitsajwan/option_trading`, branch `feat/dhan-feature-engine` (all commits pushed) |
| GCP | project `trader-502012`: VMs, disks, static IP, buckets and firewall rule **deleted**. The empty project and its two API keys remain (no cost). |
| Backup (the ONLY copy of the data) | laptop `C:\code\option_trading\gcp_backup_2026-09-29\` (5.1 GB). **Copy it to a second place** (external drive or cloud drive). |
| Broker | Dhan account **closed**. The old credentials are dead, so a new account is needed. |
| Trading | every buyer and seller halted (`operator_halt` files); zero open positions at close |

---

## Step by step: coming back

### 1. Prerequisites

- A GCP project with billing enabled. The old `trader-502012` can be reused, or
  create a new one: `gcloud projects create <id>` and link billing in the console.
- `gcloud auth login` on the laptop (Git Bash), and the repo cloned locally.
- The backup directory (above) on the laptop.

### 2. Broker credentials

For a new Dhan account, create `.env.totp` with exactly these three lines (enable TOTP
in the Dhan web console to get the secret):

```
DHAN_CLIENT_ID=<new client id>
DHAN_PIN=<login pin>
DHAN_TOTP_SECRET=<base32 TOTP secret>
```

Keep this file outside the repo. It is the single source of truth for which account
the stack uses. The token refresh writes both the client ID and a fresh access token
into `.env.compose` from it.

### 3. Rebuild

```bash
bash ops/rebuild_new_vm.sh --project <gcp-project> --totp path/to/.env.totp
# optional: --zone asia-south1-b --vm trader-runtime-01 --machine e2-standard-4 --disk 80
```

It checks the backup's checksums, enables Compute, creates the dashboard firewall
rule, reserves a static IP (`<vm>-ip`) and creates an Ubuntu 22.04 VM. It then uploads
the backup and runs `ops/restore_from_backup.sh` on the VM, which:

- installs Docker and clones the repo at the backup's `COMMIT`;
- restores secrets, models, `.run/`, parquet, the seller volumes and Mongo (897,791 documents);
- **halts everything**;
- builds the images and starts the 45 services that were running at closure;
- mints a broker token and installs the 6 scheduled jobs.

It ends by printing the dashboard URL.

- **Without `--totp`**, or if the broker rejects the credentials, the data and dashboard
  come up but the broker-connected services are stopped and no timers are installed.
  It exits with code 2 and prints the fix.
- **Then whitelist the VM's new static IP** in the broker console. Order APIs reject
  calls from non-whitelisted IPs.

### 4. Check before trading

- **Futures contract symbols in `.env.compose` will have expired** (for example
  `SENSEX26AUGFUT`). Roll them to the current month for each instrument, then
  recreate the affected services with `ops/deploy.sh`.
- Models are trained on data up to September 2026. Treat them as stale.
- Verify:

  ```bash
  gcloud compute ssh trader-runtime-01 --zone=asia-south1-b --project=<p> \
    --command="cd /opt/option_trading && sudo bash ops/vm_lifecycle.sh status"
  ```

### 5. Going live (deliberate, per instrument)

Rebuild never enables trading. To let one instrument trade, check its config first, then
remove its halt:

```bash
# buyer, e.g. NIFTY:
sudo rm /opt/option_trading/.run/strategy_app_nifty/operator_halt
# seller, e.g. SENSEX:
sudo docker exec option_trading-seller_app_sensex-1 rm -f /seller_run/operator_halt
```

`docs/EMERGENCY_STOP.md` has the halt and resume details. Real-money execution also needs
the instrument's `*_EXECUTION_ADAPTER=dhan` (and, for sellers, `*_SELLER_LIVE_ENABLED=1`)
in `.env.compose`. **Read "Research status" below first.** No strategy in this repo has
a demonstrated profitable edge.

### Deploying code, parking, resuming (once rebuilt)

- **Deploy:** commit locally, move the code with a git bundle and run `sudo bash ops/deploy.sh <services>`
  on the VM (the VM has no GitHub credentials; its `origin` is `/tmp/repo.bundle`).
- **Park cheaply:** `bash ops/shutdown.sh` (freeze, then stop the VM; the disk and IP are kept).
- **Resume from parked:** `bash ops/resume.sh [--totp file]` (about 3.5 minutes).
- If the project, zone or VM name differ from the defaults, set `PROJECT`, `ZONE` and
  `VM` in the environment for both scripts.

### Test record (2026-09-29)

- **Park and resume** on the real VM: park took about 1 minute and resume about
  3.5 minutes. Resume brought back 45/45 containers, minted a token, re-enabled the
  6 timers and passed the config contract.
- **Resume with dead credentials:** exited 2 cleanly, with broker services off and timers off.
- **Full rebuild onto a brand-new VM from the laptop backup:**
  - 45/45 containers running, 6 timers, config contract PASS, all halts present;
  - Mongo counts matched live;
  - snapshots published for all 5 instruments, and the dashboard returned 200.
- **Three bugs the rebuild test found, all fixed:**
  - Windows `pscp` does not expand `~` in remote paths;
  - a plain `compose build` skipped the profile-gated `execution_app`;
  - `compose up` aborted on health checks while the token was stale.

### Backup contents

| File | Contents |
|---|---|
| `mongo_trading_ai.archive.gz` | `mongodump` of the whole `trading_ai` DB (1.2 GB), including historical market snapshots that **cannot be re-downloaded** |
| `parquet.tar` | `.data/ml_pipeline/` (per-instrument snapshot parquet, 3.2 GB) |
| `run.tar.gz` | `.run/` (canonical JSONL trade and decision logs, training views, halt files) |
| `models.tar.gz` | `models/` (including research models not in git) |
| `secrets.tar.gz` | `.env.compose`, `.env.totp` (the Dhan ones are now dead) |
| `vol_option_trading_seller_*.tar.gz` | seller Docker volumes (seller state and halts) |
| `lifecycle/` | the services and timers running at closure (what the rebuild starts) |
| `research_scripts_uncommitted.tar.gz` | 13 ad-hoc research scripts that were never committed |
| `SHA256SUMS`, `COMMIT` | checksums, and the commit the rebuild checks out |

Mongo is restored only from the `mongodump`, never from a raw copy of its data directory.

---

## Using a broker other than Dhan

The broker is wired in at these points:
- `execution_app/adapter/dhan.py`: orders
- `ingestion_app/dhan_data_service.py` and `ingestion_app/dhan_ws_feed.py`: market data
- `ops/gcp/dhan_totp_refresh.py` and `ops/gcp/dhan_token_refresh.sh`: auth
- `strategy_app/seller/gateway.py`: seller orders

`EXECUTION_ADAPTER` already selects between `paper`, `dhan` and `kite`. A new broker
needs its own adapter and data service. Paper mode works without any broker for the
strategy side, but the data services still need a market-data source.

---

## Research status at closure (read before trading real money)

- **Option buying: no tradeable edge found** (NIFTY and BankNifty, commits `808ba5d`
  through `41e32eb`). Direction was tested more than 10 ways and is a coin flip. Stop and
  trailing exit shapes don't help. The entry-payoff model that looked validated turned
  out to be mostly an expiry-day detector: `dte` alone matches it, and its top trades
  lose money when the side is picked by coin flip.
- **Option selling: real-money record was negative**: 7 trades, net −₹4,559
  (BankNifty and NIFTY, July–Sept 2026).
- **Still untested:**
  - A volatility edge: buy only when the predicted move exceeds the IV-implied move,
    scored with a straddle-P&L label and benchmarked against `dte`.
  - A careful re-look at selling.

## Known issues at closure

- The dead-code checker `ml_pipeline_2/tests/test_boundaries.py` fails (pre-existing and
  harmless; its substring match flags the package's own imports).
- `strategy_persistence_app_nifty`, `_sensex` and `_midcpnifty` report unhealthy
  (pre-existing; they come back the same way after every resume and rebuild).
- The composite direction resolver's `vix_chg` signal never fires. Its threshold expects
  VIX points, but the field is a fraction.

## GCP leftovers

The empty project `trader-502012` still exists, with default firewall rules and two
API keys ("Gemini API Key", "event-calendar-llm"). None of these cost anything. To
remove the project entirely: `gcloud projects delete trader-502012` (recoverable for
30 days).
