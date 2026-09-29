# Resuming the trading stack

The stack was parked on **2026-09-29** when the Dhan account was closed. This page is
everything needed to bring it back.

## TL;DR

```bash
# from the repo root on your laptop (Git Bash), with gcloud logged in:
bash ops/resume.sh --totp path/to/new/.env.totp
```

That one command starts the VM, installs the new broker credentials, restarts every
service that was running at shutdown, mints a broker token, re-enables the scheduled
jobs and runs the health checks. **Every instrument comes back halted.** Nothing trades
until you remove an `operator_halt` yourself (see "Going live").

---

## State at closure (2026-09-29)

| What | Where |
|---|---|
| Code | GitHub `amitsajwan/option_trading`, branch `feat/dhan-feature-engine` (all commits pushed) |
| VM | GCP project `trader-502012`, VM `trader-runtime-01` (e2-standard-4, zone `asia-south1-b`), **stopped, not deleted** |
| Public IP | reserved static `trader-runtime-ip` = `8.231.101.82` (kept while stopped) |
| Data + credentials | on the VM's disk, unchanged (Mongo, parquet, `.run/`, models, `.env.compose`, `.env.totp`) |
| Offline backup | laptop `C:\code\option_trading\gcp_backup_2026-09-29\`, also on the VM at `/var/backups/option_trading_closure_2026-09-29/` |
| Broker | Dhan account **closed**. The old credentials are dead, so a new account is needed. |
| Trading | every buyer and seller halted (`operator_halt` files); zero open positions at close |

Parking cost is only the stopped VM's disk plus the reserved IP (a few dollars a month;
check GCP billing). To stop even that, see "Tearing down completely".

---

## Path A: resume (the VM still exists)

### 1. Get broker credentials

For a new Dhan account, create `.env.totp` with exactly these three lines (enable TOTP
in the Dhan web console to get the secret):

```
DHAN_CLIENT_ID=<new client id>
DHAN_PIN=<login pin>
DHAN_TOTP_SECRET=<base32 TOTP secret>
```

Keep this file outside the repo. It is the single source of truth for which account
the stack uses. The token refresh writes both the client ID and a fresh access token
into `.env.compose` from it, so nothing else needs editing.

In the Dhan console, **whitelist the static IP `8.231.101.82`**. Dhan's order APIs
reject calls from non-whitelisted IPs.

### 2. Run it

```bash
bash ops/resume.sh --totp path/to/.env.totp
```

- Without `--totp`, it reuses whatever `.env.totp` is already on the VM.
- If the broker rejects the credentials, the script still brings Mongo, the dashboard
  and the data services up, then stops the broker-connected services and leaves the
  scheduled jobs off, so nothing crash-loops or spams alerts. It exits with code 2 and
  tells you to re-run with valid credentials.
- On success it prints the dashboard URL: `http://8.231.101.82:8008`.

What runs on the VM is `ops/vm_lifecycle.sh thaw`. The same script does `freeze`
(parking) and `status`:

```bash
gcloud compute ssh trader-runtime-01 --zone=asia-south1-b --project=trader-502012 \
  --command="cd /opt/option_trading && sudo bash ops/vm_lifecycle.sh status"
```

### 3. Going live (deliberate, per instrument)

Resume never enables trading. To let one instrument trade, check its config first, then
remove its halt:

```bash
# buyer, e.g. NIFTY:
sudo rm /opt/option_trading/.run/strategy_app_nifty/operator_halt
# seller, e.g. SENSEX:
sudo docker exec option_trading-seller_app_sensex-1 rm -f /seller_run/operator_halt
```

`docs/EMERGENCY_STOP.md` has the halt and resume details. Real-money execution also needs
the instrument's `*_EXECUTION_ADAPTER=dhan` (and, for sellers, `*_SELLER_LIVE_ENABLED=1`)
in `.env.compose`.

**Read "Research status" below before going live.** No strategy in this repo has a
demonstrated profitable edge.

### 4. Deploying new code after resume

This is unchanged. Commit locally, move the code with a git bundle, and deploy with
`ops/deploy.sh`. The VM has no GitHub credentials, so its `origin` remote is a bundle
file:

```bash
git bundle create /tmp/repo.bundle feat/dhan-feature-engine
gcloud compute scp /tmp/repo.bundle trader-runtime-01:/tmp/repo.bundle --zone=asia-south1-b --project=trader-502012
gcloud compute ssh trader-runtime-01 --zone=asia-south1-b --project=trader-502012 \
  --command="cd /opt/option_trading && sudo bash ops/deploy.sh <services...>"
```

### Parking again

```bash
bash ops/shutdown.sh            # freeze + stop VM
bash ops/shutdown.sh --keep-vm  # freeze services only
```

---

## Path B: disaster rebuild (the VM or GCP project is gone)

Use the offline backup on the laptop: `C:\code\option_trading\gcp_backup_2026-09-29\`.

| File | Contents |
|---|---|
| `mongo_trading_ai.archive.gz` | `mongodump` of the whole `trading_ai` DB (1.2 GB), including all historical market snapshots, which **cannot be re-downloaded** once the broker account is closed |
| `parquet.tar` | `.data/ml_pipeline/` (per-instrument snapshot parquet, 3.2 GB; rebuildable from Mongo, but slow) |
| `run.tar.gz` | `.run/` (canonical JSONL trade and decision logs, training views, halt files) |
| `models.tar.gz` | `models/` (including research models not in git) |
| `secrets.tar.gz` | `.env.compose`, `.env.totp` (the Dhan ones are now dead) |
| `vol_option_trading_seller_*.tar.gz` | seller Docker volumes (seller state and halts) |
| `SHA256SUMS`, `COMMIT` | checksums; the exact commit the backup matches |

**Verified at closure:** all checksums match; every archive is readable; the Mongo
gzip stream passes its CRC check; a real restore of one collection into a scratch
database returned 1,668 of 1,668 documents, matching live.

Rebuild on any fresh Ubuntu VM (8+ GB RAM, 80+ GB disk):

```bash
# copy the backup dir to the VM, then:
curl -fsSLO https://raw.githubusercontent.com/amitsajwan/option_trading/feat/dhan-feature-engine/ops/restore_from_backup.sh
sudo bash restore_from_backup.sh ~/gcp_backup_2026-09-29
```

The script verifies checksums, installs Docker, clones the repo at the backed-up
commit, restores the files, volumes and Mongo, **halts everything**, builds and starts
the services, mints a token and installs the scheduled jobs.

**It has never been run end-to-end on a fresh VM.** Every step is scripted, and each is
a routine command. Watch the first run. Also:

- The new VM has a new IP. Whitelist it with the broker.
- If the repo has become private, clone with credentials first.
- Skip `vol_option_trading_mongo_data` if you ever produce one. Mongo is restored from
  the `mongodump`, never from a raw copy of its data directory.

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
- `strategy_persistence_app_nifty` and `strategy_persistence_app_sensex` showed as
  unhealthy before closure (pre-existing).
- The composite direction resolver's `vix_chg` signal never fires. Its threshold expects
  VIX points, but the field is a fraction.

## Tearing down completely

This is irreversible for anything not in the laptop backup: the VM, its disk and the
reserved IP.

```bash
gcloud compute instances delete trader-runtime-01 --zone=asia-south1-b --project=trader-502012
gcloud compute addresses delete trader-runtime-ip --region=asia-south1 --project=trader-502012
```

After this, only Path B can bring the stack back.
