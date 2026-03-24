# Process Topology and Runbook

Run commands from repo root: `c:\code\option_trading`

## 1. Start Mode

Use exactly one runtime path at a time:

1. Docker Compose (production-like, preferred)
2. Local launcher (`start_apps.py`) for debug/development

Do not run Compose and local launchers together.

Supported live runtime lane is `ml_pure`.
Use `deterministic` here for replay and diagnosis only.

## 2. Compose Topology

### Baseline live stack

```powershell
docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app strategy_persistence_app
```

### Optional UI profile

```powershell
docker compose --env-file .env.compose --profile ui up -d dashboard strategy_eval_orchestrator strategy_eval_ui
```

### Optional historical profile

```powershell
docker compose --env-file .env.compose --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical strategy_eval_orchestrator
```

### One-shot historical replay publisher

```powershell
docker compose --env-file .env.compose --profile historical_replay up historical_replay
```

### Safe historical UI recipe

```powershell
docker compose --env-file .env.compose --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
docker compose --env-file .env.compose --profile ui up -d dashboard
docker compose --env-file .env.compose --profile historical_replay run --rm historical_replay --start-date 2026-03-06 --end-date 2026-03-06 --speed 0
```

Use `/historical/replay` for the replay-first operator page. Keep the archived `/trading` launcher out of this flow.

### Stop

```powershell
docker compose --env-file .env.compose down --remove-orphans
```

## 3. Local Launcher Topology

Start:

```powershell
python -m start_apps --include-dashboard
```

Stop:

```powershell
python -m stop_apps --include-dashboard
```

## 4. Process Order (Live)

1. Infra: `redis`, `mongo`
2. Ingestion: `ingestion_app`
3. Snapshot producer: `snapshot_app`
4. Consumers: `persistence_app`, `strategy_app`
5. Strategy persistence: `strategy_persistence_app`
6. Optional UI: `dashboard`, `strategy_eval_ui`, `strategy_eval_orchestrator`

## 5. Health Checks and Ports

- Ingestion health: `http://127.0.0.1:8004/health`
- Dashboard health (compose UI): `http://127.0.0.1:8008/api/health`
- Dashboard health (local launcher default): `http://127.0.0.1:8002/api/health`
- Strategy eval UI: `http://127.0.0.1:8011`
- Redis default port: `6379`
- Snapshot health command:

```powershell
python -m snapshot_app.health --events-path .run/snapshot_app/events.jsonl --max-age-seconds 300
```

- Strategy health command:

```powershell
python -m strategy_app.health
```

## 6. Runtime Files and Logs

- Snapshot stream file: `.run/snapshot_app/events.jsonl`
- Strategy stream files: `.run/strategy_app/{votes.jsonl,signals.jsonl,positions.jsonl}`
- Local launcher process logs:
  - `.run/ingestion_app/{stdout.log,stderr.log,process.json,session_state.json}`
  - `.run/snapshot_app/{stdout.log,stderr.log,process.json,events.jsonl}`
  - `.run/persistence_app/{stdout.log,stderr.log,process.json}`
  - `.run/persistence_app_strategy/{stdout.log,stderr.log,process.json}`
  - `.run/dashboard/{stdout.log,stderr.log,process.json}` (local launcher)

## 7. Fail-Fast Indicators

- `ingestion_app` healthy but market collectors idle outside session: expected.
- `snapshot_app` no fresh `events.jsonl` lines during market session: treat as failure.
- `strategy_app` running but no `votes/signals` updates while snapshots are arriving: treat as failure.
- `strategy_persistence_app` running but Mongo strategy collections not updating: treat as failure.
- Any runtime ML enable without guard artifact or capped-live constraints: reject startup.
- Any `ml_pure` run-id switch without strict approval should reject startup:
  - missing `ML_PURE_RUN_ID`/`ML_PURE_MODEL_GROUP`
  - run decision not `PROMOTE`
  - missing resolved model/threshold artifacts

## 8. Quick Diagnostic Commands

```powershell
docker compose ps
docker compose logs --tail 120 ingestion_app
docker compose logs --tail 120 snapshot_app
docker compose logs --tail 120 strategy_app
docker compose logs --tail 120 strategy_persistence_app
Get-Content .run/snapshot_app/events.jsonl -Tail 5
Get-Content .run/strategy_app/signals.jsonl -Tail 5
```

## 9. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [runbooks/README.md](runbooks/README.md)
- [runbooks/GCP_DEPLOYMENT.md](runbooks/GCP_DEPLOYMENT.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)
