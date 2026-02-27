# ingestion_app

Process that starts live/historical ingestion workers and the market API.

## Ownership
- Owns ingestion runtime entrypoints and process supervision.
- Canonical modules:
  - `main_live.py`
  - `runner.py`
  - `runner_historical.py`
  - `runtime.py`
  - `collectors/*` (module entrypoint wrappers)

## Entrypoints
- `python -m ingestion_app.main_live --mode live --start-collectors` (non-blocking launcher; writes logs to `.run/ingestion_app/`)
- `python -m ingestion_app.main_live --mode live --start-collectors --foreground` (blocking/foreground)
- `python -m ingestion_app.runner --mode live --start-collectors`
- `python -m ingestion_app.runner_historical --historical-source zerodha`
- Health: `python -m ingestion_app.health --api-base http://127.0.0.1:8004`
- Stop: `python -m ingestion_app.stop`

## Package Standards
- Start subprocesses with module form only (`python -m package.module`).
- Avoid filesystem script paths in commands.
- No `sys.path` mutation in app entrypoints.

## Health Output Contract
- Health commands print one JSON object.
- Exit code: `0=healthy`, `1=degraded`, `2=unhealthy`.

## Start Safety
- Non-blocking launcher is idempotent: if already running, it returns `launcher.action=already_running` and does not start duplicates.

## Compatibility
- Legacy `market_data.runner*` and related runtime modules are shims to `ingestion_app`.
- Transitional root-split compatibility: `main_live` injects `market_data/src` into `PYTHONPATH`
  for spawned processes so existing `market_data.*` imports keep working during migration.

## Runtime Assets
- Env template: `ingestion_app/.env.example`
- PowerShell wrappers:
  - `ingestion_app/start.ps1`
  - `ingestion_app/stop.ps1`
- Docker:
  - `ingestion_app/Dockerfile`
  - Build from repo root:
    - `docker build -f ingestion_app/Dockerfile -t ingestion_app:local .`
  - Run:
    - `docker run --rm -it --name ingestion_app --env-file market_data/.env ingestion_app:local`

## Compose Session Runner (IST)
- Container entrypoint uses `python -m ingestion_app.market_session_runner --mode live`.
- During market session (`09:15-15:30 Asia/Kolkata`, trading days), wrapper starts `ingestion_app.runner --mode live --start-collectors`.
- Outside session, wrapper keeps container idle and stops live collectors.

## Token Lifecycle (Fail Closed)
- Credentials are read from `KITE_CREDENTIALS_PATH` (compose default `/app/secrets/credentials.json`).
- If token is missing/expired at open, collectors are not started (no synthetic fallback).
- Update `credentials.json` from host after manual login; wrapper auto-retries and starts when credentials become valid.
