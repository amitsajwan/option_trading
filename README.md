# option_trading

Architecture-first repository for the BankNifty options system.

Included modules:
- `contracts_app`
- `ingestion_app`
- `snapshot_app` (live + historical snapshot pipeline)
- `persistence_app`
- `strategy_app`
- `market_data_dashboard`
- `ml_pipeline`
- `docker-compose.yml` and compose env template

Intentionally excluded:
- `scripts/`
- cache/log/run artifacts
- local data/artifacts
- secrets (`credentials.json`, `.env`)

## Quick start

1. Copy env template:
   - `cp .env.compose.example .env.compose`
2. Start core stack:
   - `python -m start_apps`
3. Start core stack plus dashboard:
   - `python -m start_apps --include-dashboard`
4. Stop stack:
   - `python -m stop_apps`

See full process topology and commands in:
- `PROCESS_TOPOLOGY.md`

## Notes

- This repo is curated to keep only architecture-related components.
- Legacy runtime folders and generated data are intentionally not tracked.
