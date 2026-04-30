# Release Readiness Checklist

As-of: `2026-04-27`

Minimum release gate for:

- `strategy_app`
- `strategy_persistence_app_historical`
- `market_data_dashboard`

## Before Release

- Identify branch and commit IDs
- Identify release owner and rollback owner
- Identify target VM/environment
- Identify scope: dashboard-only, runtime-only, or full-stack

## Code Health

- Touched-module tests pass
- Replay/session tests pass for dashboard changes
- Docs match current runtime behavior

## Replay Truth

- Rerun a known historical window
- Verify same `run_id` across: summary, trades, session
- Verify no mixed-run leakage in signals/votes/diagnostics

## Dashboard Verification

- `/api/health` responds
- `/api/health/replay` responds
- `/historical/replay` renders
- `/api/historical/replay/session?date=<date>&run_id=<run_id>` returns:
  - correct `active_run_id`
  - expected `strategy_profile_id`
  - run-scoped signals/votes/trades

## Runtime Verification

- Strategy consumer lock acquired cleanly
- No duplicate consumer crash loop
- Replay emitted non-zero events
- Persistence wrote votes/signals/positions for the new run
- Expected `strategy_profile_id`:
  - Deterministic production freeze: `det_prod_v1`
  - ml_pure: `ml_pure_staged_v1`

## ml_pure Additional Checks

- Confirm `ml_pure_model_package` and `ml_pure_threshold_report` in `runtime_config.json`
- Confirm paths resolve (local files exist, or GCS URLs are reachable)
- Confirm `GCS_ARTIFACT_CACHE_DIR` is set or the default `~/.cache/option_trading_models/` is writable
- For `capped_live` rollout: guard file present and valid, `position_size_multiplier <= 0.25`

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

1. Identify last known good commit
2. Rebuild only the affected container
3. Restart only the affected container
4. Rerun the same smoke checks

## Sign-Off

Required:

- Program Manager
- System Architect
- Release Owner

If strategy behavior changed materially, add:

- Lead Trader
- Quant Research Lead
