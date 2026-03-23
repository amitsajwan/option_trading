# ingestion_app

Process that starts live ingestion API/runtime with session-aware supervision.

## Ownership
- Owns ingestion runtime entrypoints and process supervision.
- Canonical modules:
  - `main_live.py`
  - `runner.py`
  - `runtime.py`
  - `api_service.py`
  - `collectors/*` (optional placeholders, disabled by default)

## Entrypoints
- `python -m ingestion_app.main_live --mode live --start-collectors` (non-blocking launcher; writes logs to `.run/ingestion_app/`)
- `python -m ingestion_app.main_live --mode live --start-collectors --foreground` (blocking/foreground)
- `python -m ingestion_app.runner --mode live --start-collectors`
- `python -m ingestion_app.api_service`
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

## Decoupling
- `ingestion_app` does not import `market_data.*`.
- API serving is owned by `ingestion_app.api_service`.
- Shared contracts/utilities come from `contracts_app`.

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
    - `docker run --rm -it --name ingestion_app --env-file .env ingestion_app:local`

## Compose Session Runner (IST)
- Container entrypoint uses `python -m ingestion_app.market_session_runner --mode live`.
- During market session (`09:15-15:30 Asia/Kolkata`, trading days), wrapper starts `ingestion_app.runner --mode live --start-collectors`.
- Outside session, wrapper keeps container idle and stops live subprocesses.

## Token Lifecycle (Fail Closed)
- Credentials are read from `KITE_CREDENTIALS_PATH` (compose default `/app/secrets/credentials.json`).
- If token is missing/expired at open, live ingestion subprocess is not started (no synthetic fallback).
- Update `credentials.json` from host after manual login; wrapper auto-retries and starts when credentials become valid.

### Manual token refresh helper

Run this on the host to refresh the Zerodha access token into `ingestion_app/credentials.json`:

```powershell
python -m ingestion_app.kite_auth --force
```

Verification only:

```powershell
python -m ingestion_app.kite_auth --verify
```

This helper is intentionally manual. The live runtime does not trigger an interactive browser login on its own.
