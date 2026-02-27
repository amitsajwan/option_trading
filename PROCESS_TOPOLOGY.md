# Process Topology (Option Trading Repo)

Run these commands from `c:\code\option_trading`.

## Primary Start / Stop

Start core services:
```bash
python -m start_apps
```

Start core services plus dashboard:
```bash
python -m start_apps --include-dashboard
```

Start with historical replay too:
```bash
python -m start_apps --include-dashboard --include-historical
```

Stop stack:
```bash
python -m stop_apps
```

Stop stack and remove volumes:
```bash
python -m stop_apps --volumes
```

## Compose Services

Core:
- `redis`
- `mongo`
- `ingestion_app`
- `snapshot_app`
- `persistence_app`
- `strategy_app`

Optional profile `ui`:
- `dashboard`

Optional profile `historical`:
- `historical_replay`

## Startup Contract

- `start_apps` uses Docker Compose as the only startup path.
- `start_apps` supports:
  - `--include-dashboard`
  - `--include-historical`
  - `--no-build`
  - `--no-legacy-builder`
- By default, `start_apps` forces legacy builder mode:
  - `DOCKER_BUILDKIT=0`
  - `COMPOSE_DOCKER_CLI_BUILD=0`

## Session and Token Behavior

- Live flow is gated by IST market session env settings.
- If Kite credentials are missing/expired, ingestion remains idle (fail closed).
- No synthetic fallback is used for live.

## Health Checks

Use container commands after start:
```bash
docker compose --env-file .env.compose exec ingestion_app python -m ingestion_app.market_session_runner --healthcheck --state-file /app/.run/ingestion_app/session_state.json --max-stale-seconds 240
docker compose --env-file .env.compose exec snapshot_app python -m snapshot_app.health --events-path /app/.run/snapshot_app/events.jsonl --max-age-seconds 300
docker compose --env-file .env.compose exec persistence_app python -m persistence_app.health --max-age-seconds 300
docker compose --env-file .env.compose exec strategy_app python -m strategy_app.health
```
