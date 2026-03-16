# persistence_app

Consumes snapshot and strategy events and persists them to MongoDB.

## Ownership
- Owns database write path for snapshot events.
- Canonical modules:
  - `main_snapshot_consumer.py`
  - `main_strategy_consumer.py`
  - `mongo_writer.py`
  - `mongo_sink.py`

## Entrypoint
- `python -m persistence_app.main_snapshot_consumer` (non-blocking launcher; logs in `.run/persistence_app/`)
- `python -m persistence_app.main_snapshot_consumer --foreground` (blocking/foreground)
- `python -m persistence_app.main_strategy_consumer` (non-blocking launcher; logs in `.run/persistence_app_strategy/`)
- `python -m persistence_app.main_strategy_consumer --foreground` (blocking/foreground)
- Health: `python -m persistence_app.health`
- Strategy health: `python -m persistence_app.strategy_health`
- Strategy report: `python -m persistence_app.strategy_report`
- Strategy evaluation: `python -m persistence_app.strategy_evaluation`
- Stop: `python -m persistence_app.stop`

## Event Contract
- Subscribes to `contracts_app.snapshot_topic()` (`market:snapshot:v1` by default).
- Accepts `market_snapshot` v1.0 envelope only.
- Strategy consumer subscribes to:
  - `contracts_app.strategy_vote_topic()` (`market:strategy:votes:v1`)
  - `contracts_app.trade_signal_topic()` (`market:strategy:signals:v1`)
  - `contracts_app.strategy_position_topic()` (`market:strategy:positions:v1`)
  - Accepts `strategy_vote`, `trade_signal`, and `strategy_position` envelopes v1.0.
  - Health query checks `MONGO_COLL_TRADE_SIGNALS` (`trade_signals` by default).

## Time Convention
- Persists IST market fields (`trade_date_ist`, `market_time_ist`).
- Stores machine receive timestamps in IST (`received_at_ist`).
- Naive market timestamps are interpreted as IST before persistence.

## Dependency Rule
- Depends on `contracts_app` only for wire contract.
- Must not import from `snapshot_app` internals.

## Runtime Tracing
- `main_snapshot_consumer.py` emits periodic heartbeat logs (`--health-log-interval-sec`, default `30`).
- Health command prints one JSON object and exits:
  - `0=healthy`, `1=degraded`, `2=unhealthy`.

## Start Safety
- Non-blocking launcher is idempotent: if already running, it returns `launcher.action=already_running` and does not start duplicates.

## Runtime Assets
- Env template: `persistence_app/.env.example`
- PowerShell wrappers:
  - `persistence_app/start.ps1`
  - `persistence_app/stop.ps1`
  - Docker:
    - `persistence_app/Dockerfile`
  - Build from repo root:
    - `docker build -f persistence_app/Dockerfile -t persistence_app:local .`
  - Run:
    - `docker run --rm -it --name persistence_app --env-file .env persistence_app:local`

## Compose Runtime Notes
- Use foreground consumer in containers:
  - `python -m persistence_app.main_snapshot_consumer --foreground --event-topic market:snapshot:v1`
- Health check is session-aware when `MARKET_SESSION_ENABLED=1`.
- On off-market windows, stale snapshot age does not fail container health if process is alive.
- In replay/offline mode (`MARKET_SESSION_ENABLED=0`), document age is ignored; health is based on process liveness plus whether any documents have been persisted yet.

## Historical Strategy Evaluation

Evaluate persisted historical trades inside the historical strategy persistence container so the correct Mongo env and collection names are already in place:

```powershell
docker compose --profile historical exec -T strategy_persistence_app_historical `
  python -m persistence_app.strategy_evaluation `
  --date-from 2024-01-01 `
  --date-to 2024-01-31 `
  --limit 20
```

Write the report to a host file by redirecting stdout from `docker exec`:

```powershell
docker compose --profile historical exec -T strategy_persistence_app_historical `
  python -m persistence_app.strategy_evaluation `
  --date-from 2024-01-01 `
  --date-to 2024-01-31 `
  --limit 20 > .run\strategy_evaluation_2024-01.json
```

Use `--output` when running locally outside Compose, or when you explicitly want the report written inside the container filesystem.
