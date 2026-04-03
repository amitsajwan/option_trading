# Deterministic Historical Replay Runbook

Use this runbook when you need the full historical system running with UI, deterministic strategy logic, and on-demand replay windows from existing historical parquet.

This flow is for:

- full-stack deterministic replay
- dashboard visualization at `/historical/replay`
- replaying any requested `date_from` / `date_to` window
- validating current code after local changes using fresh Docker images

This is not the live production path. `deterministic` is the replay and research lane only.

## Goal

Start these components together:

- `redis`
- `mongo`
- `persistence_app_historical`
- `strategy_app_historical`
- `strategy_persistence_app_historical`
- `dashboard`
- `strategy_eval_orchestrator`

Then trigger replay windows on demand and inspect the results in the dashboard and evaluation APIs.

## Preconditions

Before starting:

1. historical snapshot parquet must already exist under `./.data/ml_pipeline/parquet_data`
2. run from repo root
3. Docker must be installed on the target host
4. if the host only has `docker-compose` v1, avoid `--force-recreate` because older Compose can fail with a `ContainerConfig` recreate error against newer Docker engines
5. do not run live Compose services and this historical stack at the same time

Expected repo root in this workspace:

```powershell
c:\code\option_trading\option_trading_repo
```

## 1. Prepare `.env.compose`

Copy the example once if needed:

```powershell
Copy-Item .env.compose.example .env.compose
```

Set deterministic mode for this session in `.env.compose`:

```dotenv
STRATEGY_ENGINE=deterministic
STRATEGY_MIN_CONFIDENCE=0.65
MARKET_SESSION_ENABLED=0
HISTORICAL_TOPIC=market:snapshot:v1:historical
```

Historical replay also needs this parquet base:

```dotenv
SNAPSHOT_PARQUET_BASE=/app/.data/ml_pipeline/parquet_data
```

Recommended historical-safe values already exist in the compose example:

- `ML_PURE_MAX_FEATURE_AGE_SEC_HISTORICAL=0`
- `STRATEGY_ML_RUNTIME_GUARD_FILE_HISTORICAL=.run/ml_runtime_guard_historical_test.json`

For deterministic replay, `ML_PURE_*` settings are ignored as long as `STRATEGY_ENGINE=deterministic`.

## 2. Rebuild Fresh Images From Current Code

When code changes locally, do not rely on old images. Rebuild before each validation pass.

Recommended rebuild command:

```powershell
docker compose --env-file .env.compose build --no-cache `
  persistence_app_historical strategy_app_historical strategy_persistence_app_historical `
  dashboard strategy_eval_orchestrator
```

If `strategy_eval_orchestrator` fails on startup with `ModuleNotFoundError: No module named 'requests'`, the image is stale. Pull the repo fix and rebuild that image again.

If you also changed shared dependencies or want a full fresh stack, include infra-adjacent services too:

```powershell
docker compose --env-file .env.compose build --no-cache `
  redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical `
  dashboard strategy_eval_orchestrator
```

## 3. Start The Full Historical Deterministic Stack

Start the historical consumers first:

```powershell
docker compose --env-file .env.compose --profile historical up -d `
  redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
```

Start the UI and replay orchestrator:

```powershell
docker compose --env-file .env.compose --profile ui up -d `
  dashboard strategy_eval_orchestrator
```

If you are on an older host with `docker-compose` v1, use `docker-compose` instead of `docker compose`.

If Compose recreate is already broken on that host, use this recovery pattern instead of retrying `--force-recreate`:

```powershell
docker rm -f option_trading_dashboard_1 option_trading_strategy_eval_orchestrator_1
docker-compose --env-file .env.compose --profile ui up -d --no-deps dashboard strategy_eval_orchestrator
docker-compose --env-file .env.compose --profile historical up -d --no-deps `
  redis mongo strategy_app_historical persistence_app_historical strategy_persistence_app_historical
```

Why this shape:

- `strategy_app_historical` listens to the historical snapshot topic
- `strategy_app_historical` defaults to `--engine ${STRATEGY_ENGINE:-deterministic}` in the historical profile
- `dashboard` reads historical Mongo collections
- `strategy_eval_orchestrator` accepts queued replay commands and publishes snapshots for requested date windows

## 4. Verify Startup

Check containers:

```powershell
docker compose ps
```

Check key logs:

```powershell
docker compose logs --tail 120 strategy_app_historical
docker compose logs --tail 120 strategy_persistence_app_historical
docker compose logs --tail 120 dashboard
docker compose logs --tail 120 strategy_eval_orchestrator
```

Dashboard health:

```powershell
Invoke-RestMethod http://127.0.0.1:8008/api/health
```

Historical replay health before a run may show idle or degraded. That is expected until you queue a replay.

## 5. Queue A Replay Window On Demand

Preferred path: queue the run through the dashboard API. This is the clean operator flow for ad hoc date-range replay.

Example for one day:

```powershell
$body = @{
  dataset   = "historical"
  date_from = "2026-03-06"
  date_to   = "2026-03-06"
  speed     = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8008/api/strategy/evaluation/runs" `
  -ContentType "application/json" `
  -Body $body
```

Example for a range:

```powershell
$body = @{
  dataset   = "historical"
  date_from = "2026-03-01"
  date_to   = "2026-03-15"
  speed     = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8008/api/strategy/evaluation/runs" `
  -ContentType "application/json" `
  -Body $body
```

Notes:

- `speed=0` means replay as fast as possible
- the orchestrator reads parquet from `SNAPSHOT_PARQUET_BASE`
- replay commands are published to `strategy:eval:command`

## 6. Visualize The Replay

Main replay UI:

```text
http://127.0.0.1:8008/historical/replay
```

Useful APIs:

- replay session: `http://127.0.0.1:8008/api/historical/replay/session?date=2026-03-06`
- replay status: `http://127.0.0.1:8008/api/historical/replay/status?date=2026-03-06`
- summary: `http://127.0.0.1:8008/api/strategy/evaluation/summary?dataset=historical&date_from=2026-03-06&date_to=2026-03-06`
- trades: `http://127.0.0.1:8008/api/strategy/evaluation/trades?dataset=historical&date_from=2026-03-06&date_to=2026-03-06`
- equity: `http://127.0.0.1:8008/api/strategy/evaluation/equity?dataset=historical&date_from=2026-03-06&date_to=2026-03-06`

What to expect in deterministic mode:

- recent votes should populate
- recent signals should populate
- deterministic diagnostics should be meaningful
- on `/historical/replay`, the votes table is the primary decision surface

## 7. Re-Run Another Date Window

You do not need to restart the stack for each date range.

Queue another replay:

```powershell
$body = @{
  dataset   = "historical"
  date_from = "2026-02-20"
  date_to   = "2026-02-28"
  speed     = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8008/api/strategy/evaluation/runs" `
  -ContentType "application/json" `
  -Body $body
```

Then refresh:

- `/historical/replay`
- `/api/historical/replay/status`
- evaluation summary/trades/equity endpoints for that exact date range

## 8. Troubleshooting

If replay queues but no results appear:

1. check `strategy_eval_orchestrator` logs
2. check `strategy_app_historical` logs for snapshot consumption
3. check `strategy_persistence_app_historical` logs for Mongo writes
4. verify parquet exists under `./.data/ml_pipeline/parquet_data`

Specific failures seen on GCP:

1. `ModuleNotFoundError: No module named 'requests'`
   This means `strategy_eval_orchestrator` was built from an older checkout. Rebuild `strategy_eval_orchestrator` from the current repo.
2. replay fails saying canonical `snapshots` parquet was not found
   Confirm `SNAPSHOT_PARQUET_BASE=/app/.data/ml_pipeline/parquet_data`.
3. browser cannot reach `:8008` even though `curl http://127.0.0.1:8008/...` works on the VM
   Confirm the VM has the `option-trading-runtime` network tag so the existing firewall rule for TCP `8008` applies.
4. `ingestion_app` name resolution errors in the Market Data Realism card
   Expected for historical-only replay when live ingestion is not running. This does not block replay or strategy evaluation.

Helpful commands:

```powershell
docker compose logs --tail 200 strategy_eval_orchestrator
docker compose logs --tail 200 strategy_app_historical
docker compose logs --tail 200 strategy_persistence_app_historical
docker compose logs --tail 200 dashboard
```

If you suspect stale code in containers:

```powershell
docker compose --env-file .env.compose down --remove-orphans
docker compose --env-file .env.compose build --no-cache `
  persistence_app_historical strategy_app_historical strategy_persistence_app_historical `
  dashboard strategy_eval_orchestrator
```

Then start again with the commands from sections 3 and 5.

## 9. Stop The Stack

```powershell
docker compose --env-file .env.compose down --remove-orphans
```

## 10. Related References

- `docs/PROCESS_TOPOLOGY.md`
- `strategy_app/docs/README.md`
- `market_data_dashboard/README.md`
- `docker-compose.yml`
