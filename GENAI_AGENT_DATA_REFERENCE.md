# GenAI Agent Data Reference

This document provides a comprehensive reference for all market data available to GenAI orchestrator agents. The system streams live market data that agents can consume via REST APIs and real-time WebSocket/STOMP subscriptions.

For canonical run instructions, see `PROCESS_TOPOLOGY.md`.

## System Overview

The runtime is source-driven with a shared pipeline:

`source adapter -> unified replay/ingestion -> Redis store -> indicators -> API (${API_PORT}) -> dashboard (${DASHBOARD_PORT}) -> UI/WS`

Current source options:

- `kite` (live)
- `historical + zerodha` (real historical replay, fail-fast)
- `historical + synthetic`
- `mock`

Canonical replay engine is `UnifiedHistoricalReplayer`.

The platform provides three main data access methods:
1. **REST APIs** (`http://127.0.0.1:${API_PORT}`) - Historical and current data
2. **Dashboard APIs** (`http://127.0.0.1:${DASHBOARD_PORT}`) - Aggregated views and status
3. **Real-time Streaming** (`ws://127.0.0.1:${DASHBOARD_PORT}/ws`) - Live updates via STOMP over WebSocket

## Connection quick reference (repo defaults + env)

Use these values as defaults for this repository. Runtime flags/env can override them.

| Component | Exact address | Notes |
|---|---|---|
| Market Data API | `http://127.0.0.1:8004` | Default API (`API_PORT`, or `-ApiPort`) |
| Dashboard API/UI | `http://127.0.0.1:8002` | Repo default dashboard port from `market_data_dashboard/.env` (`DASHBOARD_PORT`) |
| WebSocket/STOMP | `ws://127.0.0.1:8002/ws` | STOMP broker endpoint (`/ws`) on dashboard port |
| Redis (PowerShell canonical flow) | `localhost:6380` | Loaded from `market_data/.env` (`REDIS_PORT=6380`) |
| Redis (Bash fallback if not overridden) | `localhost:6379` | `start_all.sh` default unless `REDIS_PORT` is exported |

Port note:

- Canonical websocket endpoint for this stack is `ws://127.0.0.1:${DASHBOARD_PORT}/ws` (repo default: `ws://127.0.0.1:8002/ws`).
- `ws://localhost:8889/ws` is not used by this repository runtime.

### Redis port rule (important)

- If you use **`start_system.ps1`** (recommended), Redis port comes from `market_data/.env` (currently `6380`).
- If you use **`start_all.sh`**, Redis defaults to `6379` unless you set `REDIS_PORT`.

To avoid mismatches across tools, prefer one runtime path per session (PowerShell recommended on Windows).

## Canonical startup (how to run)

Use repo-root scripts as the single source of truth.

### Primary operator pattern (Live + Historical new-day)

This is the intended day-to-day workflow:

1. Run **Live** during normal operations.
2. When starting a new day from historical date, run **Historical** with the same speed profile.

Use `-HistoricalSpeed 1` as the default consistency profile.

- Live:
  - `./stop_system.ps1`
  - `./start_system.ps1 -Source kite`
- Historical (real date replay, now-like progression):
  - `./stop_system.ps1`
  - `./start_system.ps1 -Source historical -HistoricalSource zerodha -HistoricalFrom <YYYY-MM-DD> -HistoricalSpeed 1 -TimeSemantics rebase -FreshStart`

Example:

- `./start_system.ps1 -Source historical -HistoricalSource zerodha -HistoricalFrom 2026-02-13 -HistoricalSpeed 1 -TimeSemantics rebase -FreshStart`

If you intentionally want synthetic day replay, only swap `-HistoricalSource zerodha` with `-HistoricalSource synthetic`.

### Windows PowerShell (recommended)

- Live Zerodha streaming:
  - `./start_system.ps1 -Source kite`
- Historical replay (real Zerodha, fail-fast):
  - `./stop_system.ps1; ./start_system.ps1 -Source historical -HistoricalSource zerodha -HistoricalFrom 2026-02-11 -HistoricalSpeed 1 -FreshStart`
- Mock/dev mode:
  - `./start_system.ps1 -Source mock`
- Stop all:
  - `./stop_system.ps1`

### Bash / WSL (secondary)

- `./start_all.sh --source kite`
- `./start_all.sh --source historical --historical-source zerodha --historical-from 2026-02-11 --historical-speed 1`
- `./start_all.sh --source mock`
- `./stop_all.sh`

### After startup, always verify

- API health: `GET http://127.0.0.1:8004/health`
- Dashboard health: `GET http://127.0.0.1:8002/api/health`
- Mode: `GET http://127.0.0.1:8004/api/v1/system/mode`

### First data calls (copy these first)

After health is up, these calls should return usable payloads without digging through code:

1. `GET http://127.0.0.1:8004/api/v1/market/instruments`
2. `GET http://127.0.0.1:8004/api/v1/market/ohlc/{instrument}?timeframe=1min&limit=50&order=desc`
3. `GET http://127.0.0.1:8004/api/v1/technical/indicators/{instrument}?timeframe=1min`
4. `GET http://127.0.0.1:8002/api/market-data/status`
5. `GET http://127.0.0.1:8002/api/market-data/options/{instrument}`

For real-time streaming, connect STOMP to:

- `ws://127.0.0.1:8002/ws`

## What data is created by the system

At runtime, collectors/replayers generate and update four main datasets per instrument:

1. **Tick/price stream** (latest price + timestamp + quote envelope)
2. **OHLC time-series** (`1min`, `5min`, `15min`, `1h`, etc.)
3. **Technical indicators** (RSI/MACD/ATR/OI-derived metrics, etc.)
4. **Depth and options snapshots** (when available from provider)

Timeframe naming note:
- API query params may still accept aliases like `minute` / `1min` / `5min` / `15min`.
- Canonical Redis key suffixes are `1m`, `5m`, `15m`.

### Redis namespaces (mode isolation)

Data is written under mode prefixes:

- `live:*`
- `historical:*`
- `paper:*`

Customer-facing contract rule:
- Treat mode-prefixed + canonical-timeframe Redis keys as the single source of truth.
- Do not validate against unprefixed keys or `5min`/`15min` Redis key suffixes.

Examples:

- `live:ohlc_sorted:{instrument}:1m`
- `historical:ohlc_sorted:{instrument}:1m`
- `live:price:{instrument}:latest`
- `historical:price:{instrument}:latest`
- `live:depth:{instrument}:buy`
- `live:options:{instrument}:chain`

### Mode-aware key lookup (historical/live/paper) - current behavior

Yes - key-prefix changes are already handled in the current stack.

- Dashboard Redis readers prioritize the current execution mode and then fall back across mode namespaces (`live`, `historical`, `paper`).
- Market Data API indicator cache reads are mode-aware via `get_redis_key(...)`.
- Dashboard status explicitly reports `mode_mismatch` when data exists but comes from a different namespace than the current mode.

This means switching between live and historical does **not** require client-side key rewrites when using the provided HTTP/WebSocket APIs.

### If you read Redis directly (without APIs)

Do not hardcode a single prefix. Build candidates from current mode (`GET /api/v1/system/mode`) and try in this order:

1. `{current_mode}:...`
2. `live:...`
3. `historical:...`
4. `paper:...`

For example (OHLC sorted set):

- `historical:ohlc_sorted:{instrument}:1m`
- `live:ohlc_sorted:{instrument}:1m`
- `paper:ohlc_sorted:{instrument}:1m`

### Streaming events produced

The bridge publishes Redis-driven updates that appear as STOMP topics:

- `/topic/market/ohlc/{instrument}`
- `/topic/market/tick/{instrument}`
- `/topic/indicators/{instrument}`
- `/topic/market/depth/{instrument}`

This is the canonical data-creation path:

**provider/replay -> Redis keys -> API endpoints -> dashboard/WebSocket topics**

## Core Data Structures

### Market Tick
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "timestamp": "2026-02-12T09:15:00Z",
  "last_price": 45000.25,
  "volume": 1250,
  "oi": 151230,
  "oi_day_high": 152000,
  "oi_day_low": 149500
}
```

### OHLC Bar
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "timeframe": "1min",
  "open": 44950.00,
  "high": 45025.50,
  "low": 44925.75,
  "close": 45000.25,
  "volume": 1250,
  "oi": 151230,
  "start_at": "2026-02-12T09:15:00Z"
}
```

### Market Depth
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "buy": [
    {"price": 44999.00, "quantity": 100},
    {"price": 44998.50, "quantity": 250},
    {"price": 44998.00, "quantity": 150}
  ],
  "sell": [
    {"price": 45001.00, "quantity": 200},
    {"price": 45001.50, "quantity": 175},
    {"price": 45002.00, "quantity": 300}
  ],
  "timestamp": "2026-02-12T09:15:30Z"
}
```

### Options Chain
```json
{
  "instrument": "BANKNIFTY",
  "expiry": "2026-02-12",
  "futures_price": 45000.25,
  "pcr": 1.25,
  "max_pain": 44950.00,
  "strikes": [
    {
      "strike": 44500,
      "ce_ltp": 525.50,
      "ce_oi": 125000,
      "ce_volume": 25000,
      "ce_iv": null,
      "pe_ltp": 25.75,
      "pe_oi": 98000,
      "pe_volume": 18500,
      "pe_iv": null
    }
  ],
  "timestamp": "2026-02-12T09:15:00Z"
}
```

## Technical Indicators

The system calculates comprehensive technical indicators using pandas-ta. Indicators are updated on tick/candle events, and background publish cadence is configurable via `INDICATOR_PUBLISH_INTERVAL_SECONDS` (default `5`).

### Available Indicators

#### Trend Indicators
- **Moving Averages**: `sma_10`, `sma_20`, `sma_50`, `ema_10`, `ema_20`, `ema_50`, `wma_20`
- **Trend Strength**: `adx_14`, `di_plus`, `di_minus`
- **Ichimoku Cloud**: `ichimoku_tenkan`, `ichimoku_kijun`, `ichimoku_senkou_a`, `ichimoku_senkou_b`

#### Momentum Indicators
- **RSI**: `rsi_14`, `rsi_9`
- **Stochastic**: `stoch_k`, `stoch_d`
- **Williams %R**: `williams_r`
- **MACD**: `macd_value`, `macd_signal`, `macd_histogram`
- **Rate of Change**: `roc_12`
- **Momentum**: `momentum_10`

#### Volatility Indicators
- **Bollinger Bands**: `bollinger_upper`, `bollinger_middle`, `bollinger_lower`, `bollinger_width`, `bollinger_percent_b`
- **Average True Range**: `atr_14`, `atr_20`

#### Volume Indicators
- **On Balance Volume**: `obv`
- **Volume SMA**: `volume_sma_20`
- **Volume RSI**: `volume_rsi_14`
- **Chaikin Money Flow**: `cmf_20`

#### Open Interest Indicators
- **Current OI**: `oi`
- **OI Change**: `oi_change`, `oi_pct_change`
- **OI Trend**: `oi_sma_5`, `oi_ema_10`, `oi_momentum_5`

#### Oscillators
- **Commodity Channel Index**: `cci_20`
- **Money Flow Index**: `mfi_14`

#### Support/Resistance
- **Pivot Points**: `pivot_point`, `pivot_r1`, `pivot_r2`, `pivot_s1`, `pivot_s2`
- **Price Levels**: `high_20`, `low_20`, `range_20`

#### Derived Signals
- **Signal Strength**: `signal_strength` (0-100 composite score)
- **Volume Ratio**: `volume_ratio`
- **Support/Resistance Levels**: `support_level`, `resistance_level`
- **Trend Analysis**: `trend_direction` ("UP", "DOWN", "SIDEWAYS"), `trend_strength` (0-100)
- **RSI Status**: `rsi_status` ("OVERSOLD", "OVERBOUGHT", "NEUTRAL")
- **Volatility Level**: `volatility_level` ("LOW", "MEDIUM", "HIGH")

### Indicator Response Format
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "timeframe": "1min",
  "timestamp": "2026-02-12T09:15:30Z",
  "indicator_timestamp": "2026-02-12T09:15:29Z",
  "indicator_source": "calculate_indicators",
  "indicator_stream": "Y2",
  "indicator_update_type": "batch_recalculate",
  "bars_available": 17,
  "warmup_requirements": {
    "rsi": 14,
    "macd": 26,
    "bollinger": 20,
    "cci": 20,
    "stoch": 14,
    "atr": 14,
    "mfi": 14,
    "roc": 12,
    "momentum": 10,
    "adx": 14
  },
  "status": "ok",
  "indicators": {
    "rsi_14": 65.25,
    "macd_value": null,
    "bollinger_upper": null,
    "adx_14": 28.45,
    "update_type": "batch_recalculate",
    "indicator_update_type": "batch_recalculate",
    "indicator_stream": "Y2",
    "source": "calculate_indicators",
    "trend_direction": "UP",
    "signal_strength": 78.50
  }
}
```

## REST API Endpoints

### Market Data API (Port 8004)

#### Health & Status
- `GET /health` - System health check
- `GET /health/detailed` - Detailed health with validation
- `GET /diagnostics` - Comprehensive system diagnostics
- `GET /api/v1/system/mode` - Current execution mode (LIVE/HISTORICAL)

#### Market Data
- `GET /api/v1/market/tick/{instrument}` - Latest tick data
- `GET /api/v1/market/price/{instrument}` - Current price with staleness check
- `GET /api/v1/market/ohlc/{instrument}?timeframe=1min&limit=100&order=desc` - OHLC bars
- `GET /api/v1/market/instruments` - Available instruments discovered from Redis/config
- `GET /api/v1/market/depth/{instrument}` - Market depth (buy/sell orders)
- `GET /api/v1/market/raw/{instrument}?limit=100` - Raw Redis keys for instrument
- `GET /api/v1/market/overview?symbol={instrument}` - 24h overview (high/low/VWAP/change)

#### Options Data
- `GET /api/v1/options/chain/{instrument}` - Full options chain with PCR/Max Pain

#### Technical Indicators
- `GET /api/v1/technical/indicators/{instrument}?timeframe=1min` - All technical indicators + metadata (`indicator_timestamp`, `indicator_source`, `indicator_stream`, `indicator_update_type`, `bars_available`, `warmup_requirements`, `timeframe`)
- `GET /api/v1/technical/status` - Indicator service status

### Dashboard API (port is env/flag driven; repo default 8002)

#### Status & Health
- `GET /api/health` - Dashboard health
- `GET /api/market-data/health` - Market data proxy health
- `GET /api/v1/system/mode` - System mode (proxied)

#### Market Data (Proxied + Enhanced)
- `GET /api/market-data/ohlc/{instrument}?timeframe=1min&limit=100` - OHLC with auto-discovery
- `GET /api/market-data/indicators/{instrument}?timeframe=1min` - Technical indicators + stale-safe metadata (`indicator_timestamp`, `indicator_source`, `indicator_stream`, `indicator_update_type`, `bars_available`, `warmup_requirements`, `status`)
- `GET /api/market-data/instruments` - Available instruments
- `GET /api/market-data/depth/{instrument}` - Market depth
- `GET /api/market-data/options/{instrument}` - Options chain (mode-aware status + stale fallback)
- `GET /api/market-data/status` - Comprehensive status view

#### Dynamic Contract & Discovery (mode/instrument-aware)
- `GET /api/capabilities` - Runtime capabilities (`mode`, `instruments`, available topics/timeframes, endpoint/topic templates)
- `GET /api/catalog?instrument={instrument}` - Resolved Redis key catalog + availability (`redis` vs `api`) for selected instrument
- `GET /api/schema` - Versioned schema index for all public topics
- `GET /api/schema/{topic}` - JSON Schema for one topic (`mode,tick,ohlc,indicators,depth,options`)
- `GET /api/examples/{topic}?instrument={instrument}&timeframe=1m` - Latest runtime sample payload for consumer testing

### Dashboard options endpoint status semantics

`GET /api/market-data/options/{instrument}` may return:

- `status: "ok"` -> fresh options chain present
- `status: "stale"` -> last-good cached options chain served due to upstream slowness/error
- `status: "no_data"` -> no chain currently available for instrument/mode
- `status: "error"` -> unrecoverable dashboard-side failure

Additional fields that may be present:

- `warning` -> upstream timeout/error context
- `mode_hint` -> best-effort mode (`live`/`historical`/`paper`) used for user messaging

## Real-Time Streaming (STOMP over WebSocket)

### Connection Details
- **URL (local)**: `ws://127.0.0.1:8002/ws`
- **URL (same host alternative)**: `ws://localhost:8002/ws`
- **URL (if HTTPS reverse proxy is used)**: `wss://<host>/ws`
- **Protocol**: STOMP over WebSocket
- **Supported Subprotocols**: `v12.stomp`, `v11.stomp`, `v10.stomp`, `stomp`
- **Dashboard transport mode**: STOMP-only in UI (no raw/legacy websocket fallback path)

### STOMP Subscription Topics

#### Market Data Topics
- `/topic/market/ohlc/{instrument}` - All OHLC timeframes for instrument
- `/topic/market/ohlc/{instrument}/{timeframe}` - Specific timeframe OHLC
- `/topic/market/tick/{instrument}` - Live tick updates
- `/topic/market/depth/{instrument}` - Market depth updates

#### Technical Indicators
- `/topic/indicators/{instrument}` - All indicator updates for instrument

#### System Status
- `/topic/auth/status` - Authentication/connection status

### STOMP Message Format
```javascript
// Subscribe to OHLC updates
STOMP.subscribe('/topic/market/ohlc/BANKNIFTY26JANFUT', function(message) {
  const frame = JSON.parse(message.body);
  // Dashboard bridge wraps Redis payload:
  // { type, channel, data: { event envelope } }
  const envelope = frame?.data || {};
  const payload = envelope?.payload || {};
  console.log('OHLC envelope:', envelope);
  console.log('OHLC payload:', payload);
});

// Subscribe to indicators
STOMP.subscribe('/topic/indicators/BANKNIFTY26JANFUT', function(message) {
  const frame = JSON.parse(message.body);
  const envelope = frame?.data || {};
  const payload = envelope?.payload || {};
  console.log('Indicator stream:', envelope?.stream); // Y2 or LZ1
  console.log('Indicator update type:', payload?.indicator_update_type || payload?.update_type);
  console.log('Indicator payload:', payload);
});
```

### Real-Time Message Examples

#### OHLC Update
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "timeframe": "1min",
  "open": 44950.00,
  "high": 45025.50,
  "low": 44925.75,
  "close": 45000.25,
  "volume": 1250,
  "start_at": "2026-02-12T09:15:00Z"
}
```

#### Indicator Update
```json
{
  "type": "message",
  "channel": "indicators:BANKNIFTY26MARFUT:FUT",
  "data": {
    "event_id": "uuid",
    "stream": "Y2",
    "instrument": "BANKNIFTY26MARFUT",
    "timeframe": "1min",
    "event_time": "2026-02-14T11:34:07.321379+00:00",
    "emitted_at": "2026-02-14T11:34:07.323018+00:00",
    "mode": "live",
    "run_id": "",
    "schema_version": "v1",
    "payload": {
      "instrument": "BANKNIFTY26MARFUT",
      "timeframe": "1min",
      "rsi_14": 11.97,
      "macd_value": null,
      "indicator_timestamp": "2026-02-14T17:04:07.323018+05:30",
      "market_timestamp": "2026-02-14T17:04:07.321379+05:30",
      "intrabar": false,
      "candle_closed": true,
      "update_type": "batch_recalculate",
      "indicator_update_type": "batch_recalculate",
      "indicator_stream": "Y2",
      "source": "calculate_indicators",
      "bars_available": 16,
      "warmup_requirements": {
        "macd": 26,
        "bollinger": 20,
        "cci": 20,
        "stoch": 14
      }
    },
    "sequence": 1152
  },
  "timestamp": "2026-02-14T11:34:07.355495+00:00"
}
```

> Note: STOMP bridge messages are wrapped (`type`, `channel`, `data`). Indicator values are inside `data.payload`.

### Indicator reconciliation in dashboard UI (event-driven)

The dashboard uses a single event reducer path for both stream and REST indicator payloads:

1. Normalize payload shape (`STOMP` envelope `data.payload` vs `REST` nested under `indicators`).
2. Apply timeframe gating (ignore stream payloads that do not match selected timeframe, except default `1min`).
3. Apply recency gating using `indicator_timestamp`/`timestamp` (older payloads are dropped).
4. Render cards + metadata from one unified envelope.

This prevents metadata/source flicker when stream and REST updates arrive close together.

#### Tick Update
```json
{
  "instrument": "BANKNIFTY26JANFUT",
  "timestamp": "2026-02-12T09:15:45Z",
  "last_price": 45005.75,
  "volume": 1300
}
```

## Data Storage & Keys

### Redis Key Patterns

> Keys are mode-prefixed at runtime (`live:*`, `historical:*`, `paper:*`).
> Example: `historical:ohlc_sorted:BANKNIFTY26MARFUT:1min`.
> Client note: prefer API endpoints unless you intentionally need raw Redis access.
> If reading Redis directly, use mode-aware key candidate lookup (above).
> Contract rule: indicator/OHLC/depth/options keys are mode-prefixed in runtime (`live:*`, `historical:*`, `paper:*`).
> Do not treat unprefixed keys as canonical.

#### Market Data (canonical patterns)
- `{mode}:price:{instrument}:latest` - Current price
- `{mode}:price:{instrument}:latest_ts` - Price timestamp
- `{mode}:volume:{instrument}:latest` - Volume data
- `{mode}:ohlc:{instrument}:{timeframe}:{timestamp}` - Individual OHLC bars
- `{mode}:ohlc_sorted:{instrument}:{timeframe}` - Sorted set of OHLC bars
- `{mode}:depth:{instrument}:buy` - Buy depth
- `{mode}:depth:{instrument}:sell` - Sell depth

#### Technical Indicators (canonical patterns)
- `{mode}:indicators:{instrument}:{timeframe}:{indicator_name}` - Individual indicator values
- `{mode}:indicators:{instrument}:{timeframe}:timestamp` - Indicator timestamp (timeframe-scoped)
- `{mode}:indicators:{instrument}:{timeframe}:indicator_timestamp` - Canonical indicator timestamp key
- `{mode}:indicators:{instrument}:{timeframe}:source` - Indicator source/update path
- `{mode}:indicators:{instrument}:{timeframe}:indicator_stream` - Stream family (`Y2` snapshot / `LZ1` intrabar)
- `{mode}:indicators:{instrument}:{timeframe}:indicator_update_type` - Update path (`candle`, `tick`, `batch_initialize`, `batch_recalculate`)
- `{mode}:indicators:{instrument}:{timeframe}:bars_available` - Bars currently available for this timeframe

Examples (same pattern applies to all calculated indicators):
- `historical:indicators:BANKNIFTY26MARFUT:1m:rsi_14`
- `historical:indicators:BANKNIFTY26MARFUT:5m:momentum_10`
- `historical:indicators:BANKNIFTY26MARFUT:15m:roc_12`

`indicator_timestamp`/`timestamp`/`source`/`indicator_stream`/`indicator_update_type` are snapshot-level metadata for that instrument+timeframe update cycle (not separate timestamp keys per individual indicator).

Timeframe contract for Redis keys:
- Use `1m`, `5m`, `15m` (canonical)
- Do not expect `5min`/`15min` Redis keys

#### Options Data
- `{mode}:options:{instrument}:chain` - Options chain data

#### System State
- `system:execution_mode` - Current mode when set (may be absent in some runs; use `/api/v1/system/mode` as source of truth)
- `system:virtual_time:enabled` - Virtual time status
- `system:virtual_time:current` - Current virtual time

## Agent Integration Examples

### Python Agent Example
```python
import os
import requests
import json
import stomp

API_BASE = os.getenv("MARKET_DATA_API_URL", "http://127.0.0.1:8004")
WS_URL = os.getenv("DASHBOARD_WS_URL", "ws://127.0.0.1:8002/ws")

# REST API access
def get_market_data(instrument):
    response = requests.get(
        f'{API_BASE}/api/v1/market/ohlc/{instrument}?limit=50',
        timeout=5
    )
    response.raise_for_status()
    return response.json()

def get_indicators(instrument):
    response = requests.get(
        f'{API_BASE}/api/v1/technical/indicators/{instrument}?timeframe=1min',
        timeout=5
    )
    response.raise_for_status()
    return response.json()

# Real-time streaming
def on_message(frame):
    data = json.loads(frame.body)
    envelope = data.get("data", {})
    payload = envelope.get("payload", {})
    print("stream=", envelope.get("stream"))
    print("update_type=", payload.get("indicator_update_type") or payload.get("update_type"))
    print("source=", payload.get("source"))

# Connect to STOMP-over-WebSocket endpoint.
# Use a client that supports STOMP over WebSocket to connect to WS_URL.
# Example destination topics:
# - /topic/market/ohlc/BANKNIFTY26JANFUT
# - /topic/indicators/BANKNIFTY26JANFUT
```

### JavaScript/Node.js Agent Example
```javascript
const WebSocket = require('ws');
const Stomp = require('stompjs');
const API_BASE = process.env.MARKET_DATA_API_URL || 'http://127.0.0.1:8004';
const WS_URL = process.env.DASHBOARD_WS_URL || 'ws://127.0.0.1:8002/ws';

// REST API calls
async function getMarketData(instrument) {
  const response = await fetch(`${API_BASE}/api/v1/market/ohlc/${instrument}?limit=50`);
  return response.json();
}

async function getIndicators(instrument) {
  const response = await fetch(`${API_BASE}/api/v1/technical/indicators/${instrument}`);
  return response.json();
}

// Real-time streaming
const ws = new WebSocket(WS_URL, ['v12.stomp']);
const stompClient = Stomp.over(ws);

stompClient.connect({}, function(frame) {
  console.log('Connected: ' + frame);
  
  // Subscribe to OHLC updates
  stompClient.subscribe('/topic/market/ohlc/BANKNIFTY26JANFUT', function(message) {
    const data = JSON.parse(message.body);
    console.log('OHLC Update:', data);
  });
  
  // Subscribe to indicators
  stompClient.subscribe('/topic/indicators/BANKNIFTY26JANFUT', function(message) {
    const indicators = JSON.parse(message.body);
    console.log('Indicators:', indicators);
  });
});
```

## Data Update Frequencies

- **Ticks**: real-time (as received/replayed)
- **OHLC Bars**: end of each timeframe period
- **Technical Indicators**: background publisher interval is configurable via `INDICATOR_PUBLISH_INTERVAL_SECONDS` (default `5`)
- **Market Depth**: real-time (as available)
- **Options Chain**: provider/market dependent

## Error Handling

### Common HTTP Status Codes
- `200` - Success
- `404` - Instrument/data not found
- `500` - Server error
- `503` - Service unavailable (e.g., Kite credentials missing)

### WebSocket Error Handling
- Connection drops: Auto-reconnect recommended
- STOMP errors: Check subscription syntax
- Data parsing errors: Validate JSON structure

### Dashboard status levels (`/api/market-data/status`)
- `healthy`: API healthy and data flowing
- `degraded`: Redis/instrument data available but API health degraded/unreachable
- `warning`: API healthy but instrument data currently missing
- `critical`: no usable data path

Instrument entries can also report:

- `status: "available"`
- `status: "mode_mismatch"` (data exists, but from a different mode namespace)
- `status: "no_data"`

### Live mode operational notes (2026-02-13)

- Options chain for futures symbols (for example `BANKNIFTY26FEBFUT`) is fetched through underlying extraction (for example `BANKNIFTY`) in upstream API.
- Live options-chain calls can take ~10-20s depending on provider latency; dashboard proxy timeout is tuned to tolerate this window and avoid false `no_data`.
- Dashboard UI intentionally avoids tick-topic rendering load and uses throttled chart refresh.
- When websocket failures repeat, UI marks websocket as unavailable and **does not** auto-switch to REST polling fallback.

## Best Practices for Agents

1. **Use appropriate polling intervals** - Don't overwhelm APIs
2. **Cache frequently accessed data** - Redis keys persist between requests
3. **Subscribe only to needed topics** - WebSocket subscriptions are per-client
4. **Handle connection failures gracefully** - Implement reconnection logic
5. **Use indicator metadata fields** - Prefer `indicator_timestamp` + `indicator_source` for recency/provenance
6. **For STOMP indicators, parse envelope + payload** - Use `data.stream` and `data.payload.*` fields
7. **Use batch requests** when possible for multiple instruments

## Available Instruments

The system auto-discovers instruments from Redis data. Common instruments include:
- `BANKNIFTY26JANFUT` - Bank Nifty Futures
- `NIFTY26JANFUT` - Nifty Futures
- `BANKNIFTY` - Bank Nifty Index
- `NIFTY` - Nifty Index

Use `GET /api/market-data/instruments` to get currently available instruments.

---

*Last updated: February 17, 2026 (dashboard port/env defaults and startup behavior aligned with current scripts)*

*This reference covers all data structures, endpoints, and streaming topics available to GenAI agents for market analysis and trading decisions.*
