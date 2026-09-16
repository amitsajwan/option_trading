# Local Live Operations Runbook

**Owner:** BV2032 (Zerodha account)  
**Machine:** Windows 11, Docker Desktop, repo at `C:\code\option_trading\option_trading_repo`  
**Mode as of 2026-05-26:** Paper trading — no real orders placed  
**Related:** [LIVE_SETUP_GUIDE.md](LIVE_SETUP_GUIDE.md) (GCP cloud setup) · [SCRUM_BOARD_ML_ENTRY_DIRECTION.md](../SCRUM_BOARD_ML_ENTRY_DIRECTION.md)

---

## Current Config Snapshot

| Item | Value | Notes |
|------|-------|-------|
| Kite API key | in `.env` | Renew at developers.kite.trade by Jan 2027 |
| Futures instrument | `BANKNIFTY26JUNFUT` | Rolls to `BANKNIFTY26JULFUT` on **Jun 26** |
| Depth CE | `NFO:BANKNIFTY26MAY55600CE` | Expires **May 28** — update Thursday morning |
| Depth PE | `NFO:BANKNIFTY26MAY55600PE` | Expires **May 28** — update Thursday morning |
| Strategy engine | `deterministic` | `trader_master_ml_entry_v1` profile |
| Rollout stage | `paper` | No real orders — safe to run anytime |
| Depth feed | OFF (`DEPTH_FEED_ENABLED=0`) | Enable only when live profile is active |
| Capital setting | ₹5,00,000 (`RISK_CAPITAL_ALLOCATED`) | Paper only — no actual capital committed |

---

## Daily Startup (before 09:15 IST)

### Step 1 — Refresh access token (do this every morning)

Kite access tokens expire at **midnight IST**. Without a fresh token, ingestion_app cannot connect.

```powershell
cd C:\code\option_trading\option_trading_repo
python -m ingestion_app.kite_auth --force
```

Expected output:
```
[SUCCESS] Request token received: ...
Credentials saved to: ...ingestion_app\credentials.json
Credentials verified for user: BV2032
```

If the browser doesn't open automatically, copy the printed URL and open it manually in Chrome.

### Step 2 — Update access token in .env

```powershell
python -c "import json; c=json.load(open('ingestion_app/credentials.json')); print('KITE_ACCESS_TOKEN=' + c['access_token'])"
```

Copy the printed value and update `KITE_ACCESS_TOKEN=` in `.env`.

### Step 3 — Start Docker Desktop

Open Docker Desktop from Start menu. Wait until the whale icon in the taskbar stops animating (~30 sec).

### Step 4 — Start the stack

```powershell
docker compose up -d
```

Brings up: redis, mongo, ingestion_app, snapshot_app, persistence_app, strategy_persistence_app, strategy_app.

### Step 5 — Verify it's running

```powershell
# All services should show "Up"
docker compose ps

# Strategy app should show engine=deterministic and profile loaded
docker compose logs --tail 30 strategy_app

# Snapshot feed advancing (timestamps moving)
docker compose logs --tail 10 snapshot_app
```

Green signals: strategy_app logs show `engine=deterministic`, snapshot_app shows recent IST timestamps, ingestion_app shows no auth errors.

---

## Verifying Signals During the Day

Watch live signals as they arrive:

```powershell
# Tail signals file — updates every bar with entry/hold/exit decisions
Get-Content .run\strategy_app\signals.jsonl -Wait -Tail 5

# Watch votes (shadow score, trap signals firing)
Get-Content .run\strategy_app\votes.jsonl -Wait -Tail 5

# Open positions
Get-Content .run\strategy_app\positions.jsonl -Tail 20
```

Or via the dashboard (if running the `ui` profile):

```powershell
docker compose --profile ui up -d dashboard
# Open http://localhost:8008 in browser
```

---

## End-of-Day Shutdown

```powershell
docker compose down
```

This cleanly stops all containers and flushes MongoDB. Run before shutting down your machine.

To stop but keep containers defined (faster next-day restart):

```powershell
docker compose stop
```

---

## Instrument Rotation

### When to rotate

| Event | Action needed |
|-------|--------------|
| Weekly option expiry (every Thursday) | Update `DEPTH_FEED_INSTRUMENTS` in `.env` |
| Monthly futures expiry (last Thu of month) | Update `INSTRUMENT_SYMBOL` in `.env` |

### Futures rollover (monthly — next: Jun 26)

1. Check OI of current vs next month at ~10:00 IST on the 2nd-to-last Thursday:
   ```powershell
   python -c "
   from ingestion_app.kite_client import create_kite_client
   import json
   c = json.load(open('ingestion_app/credentials.json'))
   kite = create_kite_client(api_key=c['api_key'], access_token=c['access_token'])
   q = kite.quote(['NFO:BANKNIFTY26JUNFUT', 'NFO:BANKNIFTY26JULFUT'])
   for s,d in q.items(): print(s, 'OI:', d['oi'])
   "
   ```
2. When July OI exceeds June: update `.env`:
   ```
   INSTRUMENT_SYMBOL=BANKNIFTY26JULFUT
   ```
3. Restart ingestion_app and snapshot_app:
   ```powershell
   docker compose restart ingestion_app snapshot_app
   ```

### Weekly option rotation for depth feed (every Thursday morning)

The depth feed monitors ATM CE/PE bid/ask. These are weekly options that expire every Thursday.
**Must update before market opens** on expiry day (or the collector starts polling a dead contract).

1. Check spot price around 9:00 IST and round to nearest 100:
   ```powershell
   python -c "
   from ingestion_app.kite_client import create_kite_client
   import json
   c = json.load(open('ingestion_app/credentials.json'))
   kite = create_kite_client(api_key=c['api_key'], access_token=c['access_token'])
   q = kite.quote(['NFO:BANKNIFTY26JUNFUT'])
   ltp = list(q.values())[0]['last_price']
   atm = round(ltp/100)*100
   print(f'Spot: {ltp:.0f}  ATM strike: {atm}')
   "
   ```

2. Build next-expiry symbol. Format for BankNifty weekly options:
   - Same-month weekly: `NFO:BANKNIFTY26MAY{STRIKE}CE` (e.g. `NFO:BANKNIFTY26MAY55600CE`)
   - Format varies by week — check Kite instruments API or use the quote check below

3. Verify liquidity (OI must be > 100,000 on both sides):
   ```powershell
   python -c "
   from ingestion_app.kite_client import create_kite_client
   import json
   c = json.load(open('ingestion_app/credentials.json'))
   kite = create_kite_client(api_key=c['api_key'], access_token=c['access_token'])
   # Replace with new expiry symbols
   syms = ['NFO:BANKNIFTY26JUN55600CE', 'NFO:BANKNIFTY26JUN55600PE']
   q = kite.quote(syms)
   for s,d in q.items(): print(s, 'LTP:', d['last_price'], 'OI:', d['oi'])
   "
   ```

4. Update `.env`:
   ```
   DEPTH_FEED_INSTRUMENTS=NFO:BANKNIFTY26JUN55600CE,NFO:BANKNIFTY26JUN55600PE
   ```

5. Restart depth_collector (only if depth feed is enabled):
   ```powershell
   docker compose --profile live up -d --no-deps depth_collector
   ```

### Upcoming rotation schedule

| Date | Action |
|------|--------|
| **Thu May 28** | Update `DEPTH_FEED_INSTRUMENTS` to Jun 5 expiry |
| **Thu Jun 5** | Update `DEPTH_FEED_INSTRUMENTS` to Jun 12 expiry |
| **Thu Jun 26** | Roll futures: `INSTRUMENT_SYMBOL=BANKNIFTY26JULFUT` |
| **Thu Jun 26** | Update `DEPTH_FEED_INSTRUMENTS` to Jul 3 expiry |

---

## Paper → Live Promotion Checklist

**Do NOT move to live capital until all items are checked.**

### Research gate (must be verified by research team)

- [ ] OOS replay PF ≥ 1.30 with bootstrap CI lower bound ≥ 1.00 and n ≥ 40
- [ ] R1S sell-side OR a different config passes Gate 1 + Gate 2 + Gate 3 in `docs/R1S_SELLSIDE_HYPOTHESIS_2026-05-26.md`
- [ ] Edge not dependent on top-5 outlier days (top-5 share < 80%)

### Technical gate (verify in paper mode first)

- [ ] Paper mode running for ≥ 5 consecutive market days — signals appearing correctly
- [ ] JSONL files present: `positions.jsonl`, `signals.jsonl`, `votes.jsonl`
- [ ] No POSITION_OPEN without a matching POSITION_CLOSE on the same session
- [ ] Halt button verified: POST to `http://localhost:8008/api/operator/halt` → strategy stops

### Config changes for live

```
# .env changes when moving to live
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_PROFILE_ID=trader_master_live_v1
DEPTH_FEED_ENABLED=1
ML_ENTRY_BLOCK_PE=1        # CE-only until PE OOS verified
RISK_MAX_LOTS_PER_TRADE=1  # start with minimum size
```

Start depth_collector:
```powershell
docker compose --profile live up -d
```

**Paper mode does NOT place real orders.** Switching to `capped_live` will place real orders through Zerodha. Confirm all items above before making this change.

---

## Common Fixes

### Auth error in ingestion_app ("invalid api_key")

Access token expired. Run the daily token refresh (Step 1 above), update `.env`, restart:
```powershell
docker compose restart ingestion_app
```

### "No snapshot data" / snapshot_app not advancing

1. Check ingestion_app is healthy: `docker compose logs --tail 20 ingestion_app`
2. Check market hours — snapshot_app only produces data during 09:15–15:30 IST
3. Check INSTRUMENT_SYMBOL is a valid active contract (not expired)

### strategy_app shows 0 trades / no signals

Normal outside market hours. During market hours:
1. Check `votes.jsonl` — votes being logged means the engine is running
2. Check if entry time window is blocking: `ENTRY_TIME_WINDOWS` in `.env`
3. Check if VIX halt is active: `docker compose logs --tail 50 strategy_app | grep -i vix`

### Docker container fails to start

```powershell
docker compose logs <service_name> --tail 50
```

Most common causes: Redis not ready (wait 10s, retry), credentials.json missing (run kite_auth), port conflict (change port in `.env`).

### Reset state before a fresh replay

```powershell
# Stop all containers
docker compose down

# Clear JSONL run files (keeps Redis/Mongo data)
Remove-Item .run\strategy_app\*.jsonl -ErrorAction SilentlyContinue
Remove-Item .run\strategy_app_historical\*.jsonl -ErrorAction SilentlyContinue

# Start fresh
docker compose up -d
```

For a full state reset including Redis and Mongo:
```powershell
docker compose down -v    # removes named volumes (redis_data, mongo_data)
docker compose up -d
```

---

## Key File Locations

| File | Purpose |
|------|---------|
| `.env` | All runtime config — edit this for instrument/mode changes |
| `ingestion_app/credentials.json` | Kite access token — refreshed daily, never commit |
| `.run/strategy_app/signals.jsonl` | Trade signals (entry/exit decisions) |
| `.run/strategy_app/positions.jsonl` | Position lifecycle (OPEN/MANAGE/CLOSE events) |
| `.run/strategy_app/votes.jsonl` | Per-bar engine votes and shadow scores |
| `.run/strategy_app/decisions.jsonl` | Per-bar gate evaluation log (why did/didn't trade) |
| `.run/snapshot_app/events.jsonl` | Snapshot feed (market data) |

---

## Emergency Stop

If strategy_app is placing unexpected orders:

1. **Halt via UI:** `http://localhost:8008` → Halt button (top right)
2. **Or via API:**
   ```powershell
   Invoke-RestMethod -Method POST -Uri http://localhost:8008/api/operator/halt
   ```
3. **Or kill container:** `docker compose stop strategy_app`

The halt writes a sentinel file — strategy_app stops taking new positions within the current bar and holds any open position until EOD time stop.

To resume: restart strategy_app:
```powershell
docker compose start strategy_app
```

---

## GCP Token Rotation (Cloud Deployment)

When running ingestion_app on a GCP VM, the Kite access token must be refreshed daily without a browser. Three tiers of automation are available:

| Tier | Mechanism | When to use |
|------|-----------|-------------|
| **T1 — Local** | Browser auth on laptop → push to GCS | Paper mode, local dev |
| **T2 — GCP manual** | Browser auth locally → `publish_runtime_config.sh` → SSH restart | Interim / if TOTP setup not done |
| **T3 — GCP automated** | systemd timer runs TOTP auth on VM at 08:30 IST | Production / live mode |

### T3 — One-time TOTP setup on the VM

**Step 1 — Get your TOTP secret from Zerodha:**

1. Open Zerodha Console → **Settings → Security → External TOTP**
2. If already set up: choose "Reset TOTP authenticator"
3. On the setup screen choose **"Can't scan?" / "Show secret"**
4. Copy the **32-character base32 code** (looks like `JBSWY3DPEHPK3PXP...`)

> Store this secret carefully — it's equivalent to your 2FA device. If you lose it you must reset TOTP via Zerodha support.

**Step 2 — Create `.env.totp` on the VM:**

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "sudo tee /opt/option_trading/.env.totp <<'EOF'
KITE_USER_ID=BV2032
KITE_PASSWORD=<your zerodha password>
KITE_TOTP_SECRET=<32-char base32 secret>
KITE_API_KEY=anbel41tccg186z0
KITE_API_SECRET=hvfug2sn5h1xe1ky3qbuj1gsntd9kk86
EOF
sudo chmod 600 /opt/option_trading/.env.totp"
```

**Step 3 — Install the systemd timer (run once):**

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "cd /opt/option_trading && sudo bash ops/gcp/install_token_refresh_timer.sh"
```

The timer fires at **03:00 UTC (08:30 IST)** daily, before market opens at 09:15 IST. If the VM was offline at 03:00, it runs within 5 minutes of boot (`Persistent=true`).

**Step 4 — Verify the timer is installed:**

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "systemctl list-timers kite-token-refresh.timer"
```

**Step 5 — Test immediately (optional but recommended):**

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "sudo systemctl start kite-token-refresh.service && sudo journalctl -u kite-token-refresh.service -n 30 --no-pager"
```

Expected output includes:
```
Step 1: logging in as BV2032
Step 1 OK — request_id received
Step 2: submitting TOTP code 123456
Step 2 OK — request_token received
Step 3: exchanging request_token for access_token
credentials.json written to ...
Token verified for user BV2032
Token refresh OK — restarting ingestion_app
Token refresh complete
```

### Dry-run: check current TOTP code without logging in

Useful to verify `.env.totp` is readable and the TOTP secret is correct:

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "cd /opt/option_trading && source .env.totp && .venv/bin/python -m ingestion_app.kite_totp_auth --dry-run"
```

### Monitor refresh logs on VM

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b \
  --project=amit-trading \
  --command "tail -50 /var/log/kite-token-refresh.log"
```

---

## Changelog

| Date | Change |
|------|--------|
| 2026-05-26 | Initial doc. API key activated, credentials.json generated, paper mode configured. Instrument: BANKNIFTY26JUNFUT. |
| 2026-05-26 | Added GCP token rotation section: TOTP setup, systemd timer install, dry-run verification. |
