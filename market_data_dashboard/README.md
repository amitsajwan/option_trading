# Market Data Dashboard (`market_data_dashboard`)

Frontend + backend dashboard service for status monitoring, charts, and Redis-to-browser streaming.

For quick run commands by scenario, see `../README.md`.
For startup and run instructions, see [../docs/PROCESS_TOPOLOGY.md](../docs/PROCESS_TOPOLOGY.md).
For architecture and code mapping, see [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) and [../docs/DOCS_CODE_MAP.md](../docs/DOCS_CODE_MAP.md).
For stream topology and timestamp lineage, see [../docs/PROCESS_TOPOLOGY.md](../docs/PROCESS_TOPOLOGY.md).

## What this service does

- Serves dashboard UI (`/`)
- Proxies market-data HTTP endpoints
- Reads Redis directly when API endpoints are missing/slow
- Bridges Redis pub/sub to browser via STOMP-over-WebSocket (`/ws`)
- Supports the Live+Dashboard operator profile for the current runtime stack

Supported profile for this milestone:

- live monitoring pages and APIs
- live strategy session/diagnostics
- historical replay operator page and replay-health APIs
- `ml_pipeline_2` published-model discovery

Legacy / not part of the supported Live+Dashboard target:

- paper trading terminal and archived legacy launcher flows

Legacy launcher note:

- `/trading`, `/api/trading/start`, and `/api/trading/backtest/run` are opt-in only
- set `ENABLE_LEGACY_TRADING_UI=1` on the dashboard process only if you intentionally need archived paper/backtest workflows
- do not treat that launcher as part of the supported production runtime path

## Runtime dependencies

From `requirements.txt`:

- `fastapi`, `uvicorn`, `jinja2`, `requests`, `redis`, `python-dotenv`
- GCS model catalog discovery (`GCS_MODEL_ROOTS`) requires `strategy_app` to be importable and `google-cloud-storage` to be installed (used via `strategy_app.utils.gcs_artifact`). If either is missing, GCS catalog entries are silently skipped.

## Run

### Recommended (full stack)

- Start from repo root with `start_system.ps1` (Windows) or `start_all.sh` (bash)
- Canonical runtime endpoints in this mode:
	- UI/API: `http://127.0.0.1:8000`
	- WS/STOMP: `ws://127.0.0.1:8000/ws`

### Dashboard only

- `python market_data_dashboard/start_dashboard.py`

### Historical replay with dashboard

```bash
docker compose --env-file .env.compose --profile historical up -d redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
docker compose --env-file .env.compose --profile ui up -d dashboard
docker compose --env-file .env.compose --profile historical_replay run --rm historical_replay --start-date 2026-03-06 --end-date 2026-03-06 --speed 0
```

Open `/historical/replay` for replay-first operator monitoring. This flow does not require live Kite or the archived `/trading` launcher once historical snapshots already exist.

Operator/release docs for this surface:

- [`../strategy_app/docs/OPERATOR_PLAYBOOK.md`](../strategy_app/docs/OPERATOR_PLAYBOOK.md)
- [`../strategy_app/docs/RELEASE_READINESS_CHECKLIST.md`](../strategy_app/docs/RELEASE_READINESS_CHECKLIST.md)
- [`../strategy_app/docs/CURRENT_EVALUATION_BASELINE_2026-04-04.md`](../strategy_app/docs/CURRENT_EVALUATION_BASELINE_2026-04-04.md)

Environment used by dashboard:

- `DASHBOARD_HOST` (default `0.0.0.0`)
- `DASHBOARD_PORT` (default `8000`)
- `MARKET_DATA_API_URL` (default `http://localhost:8004`)
- `REDIS_HOST`, `REDIS_PORT`
- `DASHBOARD_ENABLE_DEBUG_ROUTES` (default disabled; required for `/test*` and `/simple*`)
- `DASHBOARD_LEGACY_BACKTEST_TIMEOUT_SECONDS` (default `1800`)
- `GCS_MODEL_ROOTS` — comma-separated `gs://` model directory URLs to include in the model catalog. Each URL must point to a published model directory containing `reports/training/latest.json`. Example:
  ```
  GCS_MODEL_ROOTS=gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1
  ```
  Multiple models:
  ```
  GCS_MODEL_ROOTS=gs://bucket/published_models/research/model_a,gs://bucket/published_models/research/model_b
  ```
  Requires `google-cloud-storage` and GCP credentials (consumed via `strategy_app.utils.gcs_artifact`). GCS entries appear in the model catalog (`/app?tab=models`) as research-grade entries (`card_tone=warn`, `status_label="research (gcs)"`).
- `GCS_ARTIFACT_CACHE_DIR` — local cache directory for GCS metadata files downloaded during catalog discovery (default `~/.cache/option_trading_models/`)

Port behavior note:

- In full-stack startup (`start_system.ps1`), dashboard port is explicitly set to `8000`.
- In dashboard-only startup, port follows env resolution (`DASHBOARD_PORT`); in this repo `market_data_dashboard/.env` currently sets `8002` unless overridden.

## Main endpoints

### UI pages

- `GET /` -> redirects to `/app` (302) — main SPA entry
- `GET /live/strategy` -> redirects to `/app?mode=live` (302)
- `GET /historical/replay` -> historical replay operator monitor
- `GET /strategy/evaluation` -> redirects to `/app?mode=eval` (302)
- `GET /trading` -> legacy paper trading terminal page (opt-in; requires `ENABLE_LEGACY_TRADING_UI=1`)
- `GET /trading/models` -> redirects to `/app?tab=models` (302) — model catalog
- `GET /trading?model=a|b|...` -> model-scoped terminal tab (legacy launcher)
- `GET /trading/model/{model_key}` -> redirects to model `prefill_url` or `/trading?model={model_key}` (307)

### Health

- `GET /api/health` -> dashboard health (includes `ready` flag and dependency state)
- `GET /api/health/live` -> alias for `/api/health`
- `GET /api/health/replay` -> replay-oriented health view
- `GET /api/health/strategy-runtime` -> strategy runtime artifact observability (`.run/` published state)
- `GET /api/market-data/health` -> market-data API reachability check

### Operator controls

- `GET /api/operator/halt` -> current operator halt state
- `POST /api/operator/halt` -> create the shared runtime halt sentinel
- `DELETE /api/operator/halt` -> clear the shared runtime halt sentinel

### Market data

- `GET /api/market-data/status` -> merged status view
- `GET /api/market-data/ohlc/{instrument}`
- `GET /api/market-data/charts/{instrument}` -> chart-optimized OHLC payload
- `GET /api/market-data/indicators/{instrument}`
- `GET /api/market-data/depth/{instrument}`
- `GET /api/market-data/options/{instrument}`
- `GET /api/market-data/instruments`
- `GET /api/market-data/sync-lag?instrument=...` -> Redis vs Mongo lag monitor by domain

### Live strategy

- `GET /api/live/strategy/session` -> live operator session payload
- `GET /api/live/strategy/traces` -> decision trace list
- `GET /api/live/strategy/traces/{trace_id}` -> single trace detail

### Historical replay

- `GET /api/historical/replay/session` -> historical operator session payload
- `GET /api/historical/replay/status` -> replay topic/state/progress payload
- `GET /api/historical/replay/stream` -> SSE stream of replay status updates
- `POST /api/historical/replay/generate` -> trigger replay data generation

### Strategy evaluation

- `GET /api/strategy/evaluation/summary`
- `GET /api/strategy/evaluation/equity`
- `GET /api/strategy/evaluation/days`
- `GET /api/strategy/evaluation/trades`
- `GET /api/strategy/evaluation/runs` -> list runs
- `POST /api/strategy/evaluation/runs` -> create run
- `GET /api/strategy/evaluation/runs/latest`
- `GET /api/strategy/evaluation/runs/{run_id}`

### Model catalog (always-on; legacy launcher state is separate)

- `GET /api/trading/models` -> machine-readable model catalog (includes GCS research entries)
- `GET /api/trading/model-evaluation` -> model eval snapshot (summary + training + policy reports)
- `GET /api/trading/feature-intelligence` -> feature importance / intelligence snapshot

### Legacy paper trading (opt-in; requires `ENABLE_LEGACY_TRADING_UI=1`)

- `GET /api/trading/state?instance={key}` -> per-instance runner status + positions/trades/capital
- `POST /api/trading/start` -> start paper trading runner
- `POST /api/trading/stop?instance={key}` -> stop runner
- `POST /api/trading/backtest/run` -> run one-date backtest
- `GET /api/trading/backtest/latest` -> latest backtest state

### Public contract / schema

- `GET /api/schema` -> public topic schema index
- `GET /api/schema/{topic}` -> schema for a specific topic
- `GET /api/capabilities` -> declared runtime capabilities
- `GET /api/catalog` -> runtime instrument catalog
- `GET /api/examples/{topic}` -> live example payload for a topic (ohlc, indicators, depth, options)
- `GET /api/v1/system/mode` -> current system mode
- `GET /api/v1/monitor/snapshot` -> monitor snapshot

### WebSocket

- `WS /ws` -> STOMP + legacy JSON websocket

### Debug-only endpoints

- `/test*` and `/simple*` are disabled by default in production.
- Set `DASHBOARD_ENABLE_DEBUG_ROUTES=1` only for controlled debugging sessions.

### Endpoint behavior notes

- `/api/market-data/status` is mode-aware and can mark per-instrument `mode_mismatch` when Redis data exists in a non-current namespace.
- `/api/market-data/options/{instrument}` is resilient to upstream slowness:
	- returns `status=ok` when fresh,
	- `status=stale` when serving last-good cache,
	- `status=no_data` with mode-aware message when no chain is present.
- `/api/market-data/depth/{instrument}` uses stale fallback behavior under transient upstream failures.
- `/api/market-data/indicators/{instrument}` includes metadata fields for provenance/recency:
	- `indicator_timestamp`
	- `indicator_source` (`mongo_snapshots`)
	- `indicator_stream` (`Y2` snapshot, `LZ1` intrabar)
	- `indicator_update_type` (`snapshot_event`)
	- `bars_available`
	- `warmup_requirements`
	- `timeframe`
	- `status`
- `/api/market-data/indicators/{instrument}` now reads persisted snapshots from Mongo as the canonical source (no upstream technical-indicator API dependency, no OHLC fallback path).
- `/api/market-data/sync-lag` reports Redis vs Mongo lag for `snapshot` (Redis OHLC proxy), `tick`, `depth`, and `options`, and flags domains that are Redis-only in current runtime.
- `/api/health` reports dashboard dependency state as well as process health:
  - `status` remains reachable-health oriented for launcher checks
  - `ready` reflects whether the supported operator profile is actually available
  - `dependencies` includes market-data API, Redis, and live-strategy service status

### Live Strategy Session Engine-Aware Additions

`GET /api/live/strategy/session` remains backward-compatible and now includes:

- `engine_context` (active engine mode + observed modes + strategy family/profile)
- `decision_diagnostics` with lane-specific blocks:
  - `ml_pure` (CE/PE/HOLD counts, hold-reason distribution, edge/confidence distributions)
  - `deterministic` (policy counts, block/pass ratios, warmup activity)
- `promotion_lane` (`ml_pure` or `deterministic`)

`/live/strategy` renders the active engine lane and exposes deterministic diagnostics under `decision_diagnostics.deterministic`.

### Live Strategy UX Clarity (Operator-First v1)

`GET /api/live/strategy/session` now also returns additive operator-focused blocks:

- `ops_state`:
  - `market_state`
  - `engine_state`
  - `risk_state`
  - `data_health_state`
  - `active_blocker`
- `active_alerts`: severity-ranked alert list with operator next-step hints.
- `decision_explainability`:
  - `latest_decision`
  - `timeline`
  - `gate_funnel`
  - `reason_playbook_summary`
- `ui_hints`:
  - `active_engine_panel`
  - `recommended_focus_panel`
  - `degraded_mode`
  - `debug_view`

Optional additive query params:

- `timeline_limit` (default `25`, max `100`)
- `debug_view` (`0|1`, default `0`)

Feature flag:

- `LIVE_STRATEGY_UX_V1=1` enables derived operator UX blocks in the session payload.

Alert noise tuning env vars:

- `LIVE_STRATEGY_ALERT_POLICY_BLOCK_RATE_WARN` (default `0.80`)
- `LIVE_STRATEGY_ALERT_ML_PURE_HOLD_RATE_WARN` (default `0.80`)
- `LIVE_STRATEGY_ALERT_ML_PURE_STAGE1_PRECISION_WARN` (default `0.50`)
- `LIVE_STRATEGY_ALERT_ML_PURE_PROFIT_FACTOR_WARN` (default `0.90`)
- `LIVE_STRATEGY_ALERT_ML_PURE_REGIME_DRIFT_INFO` (default `0.20`)

Rolling ML-quality inputs:

- `ML_PURE_THRESHOLD_REPORT` points to the deployed staged threshold artifact used to evaluate live Stage 1 approval precision.
- `ML_PURE_TRAINING_SUMMARY_PATH` points to the staged `summary.json` used for regime-drift baseline comparison.
- When `decision_diagnostics.ml_pure.rolling_quality` already includes `thresholds` and `breaches`, alerts prefer that persisted metadata over local env re-derivation.
- If rolling-quality evaluation is unavailable entirely, the dashboard raises an explicit monitoring-unavailable warning instead of failing silently.

### Live Monitor Module Map (v2.3 Phase-1)

The live session backend keeps `LiveStrategyMonitorService` as façade and now uses:

- `market_data_dashboard/live_strategy_repository.py` for Mongo read models/projections
- `market_data_dashboard/diagnostics/deterministic.py` for deterministic policy diagnostics
- `market_data_dashboard/diagnostics/ml_pure.py` for pure-ML diagnostics
- `market_data_dashboard/live_strategy_session_assembler.py` for engine context and final payload assembly
- `market_data_dashboard/strategy_monitor_contracts.py` for typed payload aliases

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

## Release verification checklist

Before calling the dashboard slice production-ready, verify:

1. `GET /api/health` returns `status=healthy` and `ready=true` for the supported operator profile.
2. `GET /api/live/strategy/session` returns a valid session payload for the active instrument/date.
3. `GET /api/market-data/status`, `GET /api/market-data/sync-lag`, `GET /api/market-data/instruments`, `GET /api/market-data/ohlc/{instrument}`, `GET /api/market-data/indicators/{instrument}`, `GET /api/market-data/depth/{instrument}`, and `GET /api/market-data/options/{instrument}` all return the documented top-level contract shape.
4. `GET /api/examples/ohlc`, `GET /api/examples/indicators`, `GET /api/examples/depth`, and `GET /api/examples/options` succeed, proving the public-contract router remains bound to live market-data handlers.
5. `/` and `/live/strategy` redirect correctly (to `/app` and `/app?mode=live` respectively) and the SPA loads without console errors; verify it recovers cleanly after Redis or upstream market-data restarts.
6. Debug-only endpoints remain hidden unless `DASHBOARD_ENABLE_DEBUG_ROUTES=1`.
7. If `ENABLE_LEGACY_TRADING_UI=1` is enabled intentionally, `/trading` and the legacy launcher paths are exercised separately and treated as non-core flows.

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

Last updated: 2026-04-27
