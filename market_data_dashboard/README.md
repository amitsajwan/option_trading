# Market Data Dashboard (`market_data_dashboard`)

Frontend + backend dashboard service for status monitoring, charts, and Redis-to-browser streaming.

For quick run commands by scenario, see `../README.md`.
For explicit mode/source execution instructions, see `../RUN_MODES_GUIDE.md`.
**For GenAI agent data integration**, see `../GENAI_AGENT_DATA_REFERENCE.md`.
For stream topology and timestamp lineage, see `../market_data/src/market_data/EVENT_DERIVATION_CONTRACT.md`.

## What this service does

- Serves dashboard UI (`/`)
- Proxies market-data HTTP endpoints
- Reads Redis directly when API endpoints are missing/slow
- Bridges Redis pub/sub to browser via STOMP-over-WebSocket (`/ws`)

## Runtime dependencies

From `requirements.txt`:

- `fastapi`, `uvicorn`, `jinja2`, `requests`, `redis`, `python-dotenv`

## Run

### Recommended (full stack)

- Start from repo root with `start_system.ps1` (Windows) or `start_all.sh` (bash)
- Canonical runtime endpoints in this mode:
	- UI/API: `http://127.0.0.1:8000`
	- WS/STOMP: `ws://127.0.0.1:8000/ws`

### Dashboard only

- `python market_data_dashboard/start_dashboard.py`

Environment used by dashboard:

- `DASHBOARD_HOST` (default `0.0.0.0`)
- `DASHBOARD_PORT` (default `8000`)
- `MARKET_DATA_API_URL` (default `http://localhost:8004`)
- `REDIS_HOST`, `REDIS_PORT`

Port behavior note:

- In full-stack startup (`start_system.ps1`), dashboard port is explicitly set to `8000`.
- In dashboard-only startup, port follows env resolution (`DASHBOARD_PORT`); in this repo `market_data_dashboard/.env` currently sets `8002` unless overridden.

## Main endpoints

- `GET /` -> dashboard page
- `GET /trading` -> paper trading terminal page
- `GET /trading/models` -> model catalog page (profiles + artifact health + launch links)
- `GET /trading?model=a|b|...` -> model-scoped terminal tab (separate runner instance)
- `GET /trading/model/{model_key}` -> redirect to `/trading?model={model_key}`
- `GET /api/health` -> dashboard health
- `GET /api/market-data/status` -> merged status view
- `GET /api/market-data/ohlc/{instrument}`
- `GET /api/market-data/indicators/{instrument}`
- `GET /api/market-data/depth/{instrument}`
- `GET /api/market-data/options/{instrument}`
- `GET /api/market-data/instruments`
- `GET /api/trading/state?instance={key}` -> per-instance paper runner status + positions/trades/capital
- `GET /api/trading/models` -> machine-readable model catalog for UI/automation
- `POST /api/trading/start` -> start paper trading runner (payload supports `instance`)
- `POST /api/trading/stop?instance={key}` -> stop paper trading runner for that instance
- `WS /ws` -> STOMP + legacy JSON websocket

### Endpoint behavior notes

- `/api/market-data/status` is mode-aware and can mark per-instrument `mode_mismatch` when Redis data exists in a non-current namespace.
- `/api/market-data/options/{instrument}` is resilient to upstream slowness:
	- returns `status=ok` when fresh,
	- `status=stale` when serving last-good cache,
	- `status=no_data` with mode-aware message when no chain is present.
- `/api/market-data/depth/{instrument}` and `/api/market-data/indicators/{instrument}` also use stale fallback behavior under transient upstream failures.
- `/api/market-data/indicators/{instrument}` includes metadata fields for provenance/recency:
	- `indicator_timestamp`
	- `indicator_source`
	- `indicator_stream` (`Y2` snapshot, `LZ1` intrabar)
	- `indicator_update_type` (`candle`, `tick`, `batch_initialize`, `batch_recalculate`)
	- `bars_available`
	- `warmup_requirements`
	- `timeframe`
	- `status`

## STOMP topic mapping

Dashboard maps STOMP destinations to Redis channels/patterns:

- `/topic/auth/status` -> `auth:status`
- `/topic/market/ohlc/{instrument}` -> `market:ohlc:{instrument}:*`
- `/topic/market/ohlc/{instrument}/{timeframe}` -> `market:ohlc:{instrument}:{timeframe}`
- `/topic/market/tick/{instrument}` -> `market:tick:{instrument}:*`
- `/topic/indicators/{instrument}` -> `indicators:{instrument}:*`

It supports STOMP subprotocols (`v12.stomp`, `v11.stomp`, `v10.stomp`, `stomp`).

STOMP payload contract:

- WebSocket frames from the dashboard bridge are wrapped as `{type, channel, data}`.
- For indicator topics, use `data.payload` for indicator values and metadata (`indicator_source`, `indicator_stream`, `indicator_update_type`).

## Redis integration notes

- Dashboard uses **sync Redis pubsub in a background thread** for WS forwarding.
- This avoids issues observed with async pubsub in some Windows environments.
- Instrument list can be auto-discovered from Redis `ohlc_sorted` keys.

## UI behavior highlights

- Auto-selects an instrument that is actually available in Redis
- Throttles/coalesces chart refreshes under streaming load
- On repeated websocket failures, shows WS unavailable state (no automatic REST polling fallback)
- Market depth and options are fetched after instrument selection (including auto-selection on first load)
- In live mode, options-chain fetches may take ~10-20s depending on provider response times
- Tick-topic rendering load is intentionally minimized to keep websocket stable
- Shows an **Indicator Metadata** panel with calculated timestamp, source, timeframe, update type, market timestamp, mode, and status
- Metadata semantics:
	- `Source` = calculation/update origin (`indicator_source`)
	- `Stream` = transport stream (`indicator_stream`, typically `Y2` or `LZ1`)

## Troubleshooting

### WebSocket connected but no chart updates

Check:

1. Redis pub/sub channels are active (`market:ohlc:*`)
2. UI subscribed to correct instrument topic
3. `/api/market-data/ohlc/{instrument}` returns data
4. Ensure client uses `ws://127.0.0.1:8000/ws` (or `ws://localhost:8000/ws`) in full-stack mode, not port `8889`

### Dashboard health fails

Check:

- `.run/dashboard.log`
- `.run/dashboard.err`
- `MARKET_DATA_API_URL` reachability

---

Last updated: 2026-02-14
