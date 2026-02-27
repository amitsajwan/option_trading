# option_trading

Architecture-first repository for the BankNifty options system.

Included modules:
- `contracts_app`
- `ingestion_app`
- `snapshot_app` (live + historical snapshot pipeline)
- `persistence_app`
- `strategy_app`
- `docker-compose.yml` and compose env template

Intentionally excluded:
- `market_data/`
- `scripts/`
- cache/log/run artifacts
- local data/artifacts
- secrets (`credentials.json`, `.env`)

## Quick start

1. Copy env template:
   - `cp .env.compose.example .env.compose`
2. Start stack:
   - `docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app`

## Notes

- This repo is curated to keep only architecture-related components.
- Legacy runtime folders and generated data are intentionally not tracked.
