# Release Readiness Checklist

As-of date: `2026-04-04`

## Purpose

Minimum release gate for:

- `strategy_app`
- `strategy_persistence_app_historical`
- `market_data_dashboard`

## Before Release

- identify branch and commit ids
- identify release owner
- identify rollback owner
- identify target VM/environment
- identify whether this is dashboard-only, runtime-only, or full-stack

## Code Health

- touched-module tests pass
- replay/session tests pass for dashboard changes
- docs match current runtime behavior

## Replay Truth

- rerun a known historical window
- verify same `run_id` across:
  - summary
  - trades
  - session
- verify no mixed-run leakage in signals/votes/diagnostics

## Dashboard Verification

- `/api/health` responds
- `/api/health/replay` responds
- `/historical/replay` renders
- `/api/historical/replay/session?date=<date>&run_id=<run_id>` returns:
  - correct `active_run_id`
  - expected `strategy_profile_id`
  - run-scoped signals/votes/trades

## Runtime Verification

- strategy consumer lock acquired cleanly
- no duplicate consumer crash loop
- replay emitted non-zero events
- persistence wrote votes/signals/positions for the new run
- expected deterministic `strategy_profile_id` for production freeze: `det_prod_v1`

## Deployment

### Dashboard

```bash
cd /opt/option_trading
git pull --ff-only origin <branch>
docker-compose --env-file .env.compose build --no-cache dashboard
docker rm -f option_trading_dashboard_1
docker-compose --env-file .env.compose --profile ui up -d --no-deps dashboard
```

### Historical persistence

```bash
cd /opt/option_trading
git pull --ff-only origin <branch>
docker-compose --env-file .env.compose build --no-cache strategy_persistence_app_historical
docker rm -f option_trading_strategy_persistence_app_historical_1
docker-compose --env-file .env.compose --profile historical up -d --no-deps strategy_persistence_app_historical
```

### Historical runtime

```bash
cd /opt/option_trading
git pull --ff-only origin <branch>
docker-compose --env-file .env.compose build --no-cache strategy_app_historical
docker rm -f option_trading_strategy_app_historical_1
docker-compose --env-file .env.compose --profile historical up -d --no-deps strategy_app_historical
```

## Smoke Checks

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
curl -s http://127.0.0.1:8008/api/health
curl -s http://127.0.0.1:8008/api/health/replay
```

```bash
curl -s -X POST http://127.0.0.1:8008/api/strategy/evaluation/runs \
  -H "Content-Type: application/json" \
  -d '{"dataset":"historical","date_from":"2024-01-02","date_to":"2024-01-05","speed":0}'
```

```bash
curl -s "http://127.0.0.1:8008/api/strategy/evaluation/summary?dataset=historical&date_from=2024-01-02&date_to=2024-01-05&run_id=<run_id>"
curl -s "http://127.0.0.1:8008/api/strategy/evaluation/trades?dataset=historical&date_from=2024-01-02&date_to=2024-01-05&run_id=<run_id>"
curl -s "http://127.0.0.1:8008/api/historical/replay/session?date=2024-01-05&run_id=<run_id>"
```

## Rollback

1. identify last known good commit
2. rebuild only affected container
3. restart only affected container
4. rerun the same smoke checks

## Sign-Off

Required:

- Program Manager
- System Architect
- Release Owner

If strategy behavior changed materially, add:

- Lead Trader
- Quant Research Lead
