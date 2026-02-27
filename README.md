# BankNifty Options Algo - Repo Guide

This repo is a microservice-based trading data platform with live and historical snapshot pipelines.

If you are new, follow this reading order:

1. [ARCHITECTURE.md](ARCHITECTURE.md)
2. [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
3. Service-level READMEs (linked below)

## Services

- `ingestion_app`: market data API + session-aware live runner
- `snapshot_app`: builds and publishes canonical MarketSnapshot (MSS.1-MSS.9)
- `persistence_app`: consumes snapshot events and writes to MongoDB
- `strategy_app`: consumes snapshots for deterministic/ML strategy logic
- `market_data_dashboard` (optional): UI + monitoring APIs
- `contracts_app`: shared contracts (topics/events/session/math)

## Runtime Profiles

- Baseline live stack: `redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`
- Optional dashboard profile: `dashboard`
- Optional historical replay profile: `historical_replay`

## Quick Start (Docker Compose)

```bash
cp .env.compose.example .env.compose
docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app
```

Optional dashboard:

```bash
docker compose --env-file .env.compose --profile ui up -d dashboard
```

Optional historical replay:

```bash
docker compose --env-file .env.compose --profile historical up historical_replay
```

## Local Start (No Compose)

Use one local launcher path:

```bash
python -m start_apps --include-dashboard
```

Stop:

```bash
python -m stop_apps --include-dashboard
```

## Key Runtime Rules

- Session-gated live processing (IST market hours).
- Fail-closed token behavior (invalid/missing token keeps ingestion idle).
- Live and historical topics are isolated.
- Snapshot builder contract is centralized in `snapshot_app.market_snapshot`.

## Service Docs

- [ingestion_app/README.md](ingestion_app/README.md)
- [snapshot_app/README.md](snapshot_app/README.md)
- [persistence_app/README.md](persistence_app/README.md)
- [strategy_app/README.md](strategy_app/README.md)
- [market_data_dashboard/README.md](market_data_dashboard/README.md)
