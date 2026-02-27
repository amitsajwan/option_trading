# persistence_app

Consumes snapshot events and persists them to MongoDB.

## Ownership
- Owns database write path for snapshot events.
- Canonical modules:
  - `main_snapshot_consumer.py`
  - `mongo_writer.py`
  - `mongo_sink.py`

## Entrypoint
- `python -m persistence_app.main_snapshot_consumer` (non-blocking launcher; logs in `.run/persistence_app/`)
- `python -m persistence_app.main_snapshot_consumer --foreground` (blocking/foreground)
- Health: `python -m persistence_app.health`
- Stop: `python -m persistence_app.stop`

## Event Contract
- Subscribes to `contracts_app.snapshot_topic()` (`market:snapshot:v1` by default).
- Accepts `market_snapshot` v1.0 envelope only.

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
