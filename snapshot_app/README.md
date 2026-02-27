# snapshot_app

Builds and publishes canonical `MarketSnapshot` (MSS.1-MSS.9) events.

## Ownership
- Owns snapshot assembly runtime and publishing.
- Canonical builder lives in `snapshot_app/market_snapshot.py`.
- `main_live.py` emits one event per new `snapshot_id`.

## Entrypoint
- `python -m snapshot_app.main_live --instrument BANKNIFTY26MARFUT` (non-blocking launcher; logs in `.run/snapshot_app/`)
- `python -m snapshot_app.main_live --instrument BANKNIFTY26MARFUT --foreground` (blocking/foreground)
- Health: `python -m snapshot_app.health --events-path .run/snapshot_app/events.jsonl`
- Live snapshot quality check: `python -m snapshot_app.live_validate --events-path .run/snapshot_app/events.jsonl --tail 500`
- Stop: `python -m snapshot_app.stop`

## Historical Snapshot Build
- Historical Layer-2 builder: `python -m snapshot_app.historical.snapshot_batch_runner`
- Full user guide: `snapshot_app/historical/README.md`
- This path reuses `snapshot_app/market_snapshot.py` to build MSS.1-MSS.9 from parquet Layer-1 input.

## Data and Time Convention
- Session/business timestamps are IST-focused.
- `snapshot_id` and session fields are derived from IST market time.
- Event metadata includes `session_timezone=IST`.

## Dependencies
- Allowed: `contracts_app` for event envelope/topic contracts.
- Avoid depending on `persistence_app`.

## Runtime Tracing
- `main_live.py` emits periodic heartbeat logs (`--health-log-interval-sec`, default `30`).
- Health command prints one JSON object and exits:
  - `0=healthy`, `1=degraded`, `2=unhealthy`.

## Start Safety
- Non-blocking launcher is idempotent: if already running, it returns `launcher.action=already_running` and does not start duplicates.

## Runtime Assets
- Env template: `snapshot_app/.env.example`
- PowerShell wrappers:
  - `snapshot_app/start.ps1`
  - `snapshot_app/stop.ps1`
- Docker:
  - `snapshot_app/Dockerfile`
  - Build from repo root:
    - `docker build -f snapshot_app/Dockerfile -t snapshot_app:local .`
  - Run (dashboard API mapped via host):
    - `docker run --rm -it --name snapshot_app --env-file market_data/.env snapshot_app:local`

## Compose Runtime Notes
- Foreground command for containers:
  - `python -m snapshot_app.main_live --foreground --market-api-base http://ingestion_app:8004`
- Session gate envs:
  - `MARKET_SESSION_ENABLED=1`
  - `MARKET_TIMEZONE=Asia/Kolkata`
  - `MARKET_OPEN_TIME=09:15`
  - `MARKET_CLOSE_TIME=15:30`
  - `NSE_HOLIDAYS_FILE=/app/config/nse_holidays.json`
  - `IDLE_SLEEP_SECONDS=60`
- Off-market behavior: loop idles and emits heartbeat logs only.
