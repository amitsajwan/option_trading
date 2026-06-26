# Dhan API — Full Migration Analysis
**Date:** 2026-06-25  
**Trigger:** Kite API subscription expiring; move fully from Kite to Dhan  
**Scope:** Replace Kite for live ingestion + historical ML data; Dhan execution already live

> ⚠️ **This is the API reference. For current status, the end-to-end architecture, and team work
> assignments, see [DHAN_MIGRATION_EXECUTION_PLAN.md](./DHAN_MIGRATION_EXECUTION_PLAN.md) — it
> supersedes this doc where they disagree.** In particular, the "Only `ingestion_app/` changes"
> claim below (§7, §Summary) is **outdated**: we unified the feature computation into one
> `feature_engine.py` shared by training and all live paths, which also changed `snapshot_app/core/`
> and `strategy_app/ml/` (deliberately — to eliminate train/serve skew).

---

## 1. What Kite Currently Does for Us

Before designing the replacement, this is exactly what the running system uses Kite for:

| Layer | Kite role | Files |
|---|---|---|
| Authentication | TOTP auto-refresh at 08:30 IST daily | `ingestion_app/kite_totp_auth.py`, `token_refresh.py` |
| Live feed | WebSocket push — futures tick (LTP, OI, volume) + options chain REST poll | `ingestion_app/collectors/` |
| Option chain | REST poll every 60s — LTP, OI, IV per strike (±25 ATM) | `ingestion_app/ltp_collector.py` |
| Market depth | 5-level bid/ask for ATM CE + PE | `ingestion_app/collectors/depth_collector.py` |
| VIX | Subscribed as instrument on WebSocket | `ingestion_app/` |
| Historical (training) | Not used — training data was forward-collected into Mongo/parquet 2020–2024 | `snapshot_app/` |

**Fields we actually use from Kite in each snapshot:**

```
Futures:
  fut_open, fut_high, fut_low, fut_close, fut_volume, fut_oi
  fut_return_1m/3m/5m/15m/30m  (derived from close series)
  realized_vol_30m              (derived)

Options chain per strike (CE + PE):
  last_price (LTP)
  oi, volume
  iv (implied volatility)
  bid[0..4], ask[0..4]         (5-level depth — ATM CE and PE only)

Market-level:
  vix (India VIX, real-time)
  pcr = sum(PE OI) / sum(CE OI)      (computed)
  max_pain                             (computed from chain)
  atm_strike                           (computed)
```

All of these are assembled by `snapshot_app/` into the 1-minute snapshots that feed the strategy engine.

---

## 2. What Dhan Provides — Verified APIs

### 2.1 Authentication — Key Finding: CAN BE FULLY AUTOMATED

**TOTP-based token generation (same pattern as Kite):**
```
POST https://auth.dhan.co/app/generateAccessToken
Body:
  dhanClientId  : your client ID
  pin           : 6-digit numeric Dhan PIN
  totp          : current TOTP code (same TOTP secret as login)

Returns: access token (24h validity)
```

This means we can auto-generate a fresh token at 08:30 IST daily using the same TOTP infrastructure we already built for Kite. No manual step needed.

**Token renewal (extends without regenerating):**
```
POST https://api.dhan.co/v2/RenewToken
Headers: access-token, dhanClientId
Effect: Adds 24 hours to current token; invalidates old token
```

Use case: Call this at 08:30 IST instead of full regen — simpler if the token is still valid.

**Request headers for all Dhan API calls:**
```
access-token: {JWT}          ← NOT "Bearer {JWT}", raw token only
dhanClientId: {Client ID}
Content-Type: application/json
```

---

### 2.2 Live Market Data

#### Option Chain (REST, real-time)
```
GET https://api.dhan.co/v2/optionchain
Params:
  UnderlyingScripId  : security ID of the underlying (e.g., BANKNIFTY index ID)
  UnderlyingSeg      : "IDX_I"
  Expiry             : "YYYY-MM-DD"

Rate limit: 1 request per 3 seconds

Response per strike (CE and PE):
  ltp              ← same as Kite last_price
  oi               ← open interest
  oiDayAgo         ← previous day OI (useful for OI change)
  volume
  impliedVolatility ← IV
  delta, theta, gamma, vega  ← Greeks (Kite does NOT provide these)
  bidPrice, bidQty
  askPrice, askQty
  securityId       ← needed for WebSocket subscription
```

**Advantage vs Kite:** Greeks included. Kite gives no Greeks — we currently compute approximations or ignore them. Delta in particular is useful for strike selection and position sizing.

#### Futures Tick (WebSocket — preferred for 1-min bars)
```
wss://api-feed.dhan.co?version=2&token={JWT}&clientId={clientId}&authType=2

Subscription message (JSON):
{
  "RequestCode": 15,         ← Subscribe
  "InstrumentCount": 2,
  "InstrumentList": [
    {"ExchangeSegment": "NSE_FNO", "SecurityId": "48462"},   ← BANKNIFTY fut
    {"ExchangeSegment": "IDX_I",   "SecurityId": "13"}       ← India VIX
  ]
}

Mode options:
  15 = Ticker (LTP only, minimal bandwidth)
  17 = Quote  (full OHLC, volume, avg price, bid/ask)
  21 = Full   (Quote + 5-level market depth + OI)

Response: binary (Little Endian), 8-byte header + payload
```

**Connection limits:**
- 5 WebSocket connections per account
- 5,000 instruments per connection
- 100 instruments per subscription message (batch them)

**What to subscribe:**
- BANKNIFTY Jul fut → Mode 21 (Full: OHLC + depth + OI)
- India VIX → Mode 17 (Quote: price only)
- ATM CE + PE (current expiry) → Mode 21 (Full)

#### Market Quote REST (fallback / option chain depth)
```
POST https://api.dhan.co/v2/marketfeed/quote
Body: list of {securityId, exchangeSegment}
Rate limit: 1 req/sec, up to 1000 instruments per request

Returns: OHLC + 5-level depth + OI + avg price + circuit limits
```

Use this for the full option chain depth poll (all strikes once per minute), with WebSocket handling the real-time price tick.

---

### 2.3 VIX Data

India VIX is a first-class instrument in Dhan:
- **Security ID:** 13, Segment: `IDX_I`
- Available on WebSocket (real-time)
- Available in historical API (daily + intraday)
- Same treatment as Nifty/BankNifty — no special endpoint needed

**Current problem with Kite VIX:** We had a bug where VIX was using the wrong snapshot key (found June 21). Dhan's cleaner API should eliminate this.

---

### 2.4 India VIX Intraday Change (Key Signal)

Our shadow scorer uses `vix_intraday_chg` (VIX % change from open). With Dhan:
- Subscribe VIX on WebSocket → get real-time LTP
- Compute `(vix_ltp - vix_open) / vix_open * 100` in `snapshot_app`
- VIX open = first bar of the session (09:15 bar)

This is what we already do with Kite — identical approach, different source.

---

## 3. Historical Data — The ML Training Opportunity

This is the biggest unlock from moving to Dhan.

### 3.1 Current Training Data Situation

- Parquet: `snapshots_ml_flat_v2/` — 1199 days, 2020–2024-10, 638 MB
- Forward-collected via Kite WebSocket running continuously since ~2022
- **Gap:** No expired option chain data with IV/OI — only current-expiry data was collected
- Training coverage: morning hours weak (velocity features NaN-filled before 11:30)

### 3.2 What Dhan Expired Options API Provides

```
POST https://api.dhan.co/v2/optionchain/expiredOptions

Request:
  instrument    : "BANKNIFTY"
  expiryDate    : "YYYY-MM-DD"
  strikeRange   : 10     ← ATM ± 10 strikes
  startDate     : "YYYY-MM-DD"
  endDate       : "YYYY-MM-DD"   ← max 30 days per request

Response per bar per strike:
  open, high, low, close   ← 1-minute OHLC of the option premium
  impliedVolatility
  openInterest
  volume
  spotPrice                ← underlying price at that bar
```

**What this means for training:**
- 5 years of **expired** option data: 2021–2026
- 1-minute granularity with OI + IV — exactly what our snapshot features need
- ATM ± 10 strikes = all relevant strikes covered
- Need 30-day request chunks → ~60 API calls for 5 years of monthly expiries

**Features we can backfill using this data:**
- `atm_ce_ltp`, `atm_pe_ltp` — raw premiums
- `atm_ce_oi`, `atm_pe_oi` — OI
- `pcr` (computed)
- `atm_iv` (IV directly provided)
- `iv_regime` (classify from IV)
- `ce_pe_oi_diff`, `ce_pe_volume_diff` (our model's current features)

**What we CAN additionally derive (correction: OI IS per 1-minute bar):**
- `oi_change_1m = oi[t] - oi[t-1]` — computable ✓
- `oi_velocity_5m/10m/30m` (rolling OI build/unwind rate) — computable ✓
- CE vs PE OI divergence patterns — computable ✓
- All our existing `ce_pe_oi_diff`, `oi_change_*` features — covered

**What we CANNOT get from this API:**
- Sub-minute (tick-level) OI — but our features are all 1-min resolution so irrelevant
- Market depth (bid/ask ladder) — only OHLC per bar
- Velocity/momentum features — derivable from 1-min price series (same code as today)

### 3.3 Intraday Historical (Futures, VIX)

```
POST https://api.dhan.co/v2/charts/intraday
Body:
  securityId       : e.g., "13" for VIX
  exchangeSegment  : e.g., "IDX_I"
  instrument       : "INDEX" / "FUTIDX" / "OPTIDX"
  interval         : "1"/"5"/"15"/"25"/"60"  (minutes)
  fromDate         : "YYYY-MM-DD"
  toDate           : "YYYY-MM-DD"   ← max 90-day window per request

Response: {open, high, low, close, volume, timestamp}[]
```

**For 5 years of 1-minute BankNifty futures:** ~20 API calls (90-day chunks)  
**For 5 years of VIX:** same chunking pattern

**Daily historical (no chunking limit):**
```
POST https://api.dhan.co/v2/charts/historical
Returns full history back to instrument inception
```

### 3.4 New Training Dataset Plan

With Dhan APIs, we can build a new parquet dataset that **replaces and supersedes** `snapshots_ml_flat_v2`:

| Feature group | Source | Years available |
|---|---|---|
| Futures OHLCV | `/v2/charts/intraday` | 5 years |
| VIX intraday | `/v2/charts/intraday` | 5 years |
| ATM option premiums + OI + IV | `/v2/optionchain/expiredOptions` | 5 years |
| PCR, max_pain, atm_strike | Computed from above | 5 years |
| Returns, realized vol | Derived from OHLC series | 5 years |
| OI velocity (bar-to-bar OI change) | Derived from per-bar `openInterest` | **NEW — better than current 30-min OI** |
| Greeks (delta, theta) | `/v2/optionchain/expiredOptions` | **NEW — not in current dataset** |

**Gap vs current data:** Mid-level features like `velocity_enrichment` (11:30-anchored) and `orb_*` (opening range) need the per-bar price series — computable from intraday OHLC but require reconstruction code. The `session_context` features (time-of-day, session phase) are trivially recomputed.

---

## 4. Rate Limits — Can We Match Current Polling Frequency?

| Operation | Kite | Dhan | Verdict |
|---|---|---|---|
| Option chain poll | ~10 req/min (one per symbol) | 1 req per 3 sec (~20/min) | ✓ More than enough |
| Depth poll (ATM only) | ~12 req/min | WebSocket (unlimited) | ✓ Better via WS |
| Historical intraday | N/A | 5 req/sec | ✓ Fine for backfill |
| Expired options | N/A | 5 req/sec, 30-day chunks | ✓ Backfill in hours |
| WebSocket instruments | ~200 via Kite | 5,000 per connection | ✓ Much more capacity |

Our current snapshot assembly polls the option chain every ~60 seconds for ~50 strikes. The Dhan option chain endpoint (1 req per 3 sec) is more than sufficient — we only need 1 call per expiry per poll cycle.

---

## 5. Gaps and What Needs Building

### 5.1 WebSocket Binary Parser

Kite WebSocket returns structured JSON/protobuf (handled by `kiteconnect` SDK).  
Dhan WebSocket returns **binary Little Endian** format. Options:
1. Use the official `dhanhq` Python SDK (`MarketFeed` class) — handles binary parsing
2. Write a custom parser (documented format, ~50 lines)

**Recommendation:** Use `dhanhq.MarketFeed` for initial migration; custom parser only if we need lower latency.

### 5.2 Security ID Mapping

Kite uses instrument tokens (e.g., `256265` for Nifty 50).  
Dhan uses Security IDs (e.g., `13` for India VIX).

**One-time setup:**
```
GET https://api.dhan.co/v2/instrument/{exchangeSegment}
Returns: CSV with tradingSymbol, securityId, expiry, strike, optionType
```

Download daily or cache. BankNifty options have a new security ID per strike per expiry — need to look up from this CSV each morning.

### 5.3 Option Chain Polling Cadence Change

Current Kite approach: WebSocket push for futures tick + separate REST for option chain.  
Dhan approach: **Same** — WebSocket for futures + VIX tick; REST `optionchain` for full chain once per minute.

The snapshot_app's assembly loop does not change materially.

### 5.4 Snapshot Fields That Need Updating

Our `SnapshotAccessor` reads fields like `atm_ce_iv`, `pcr`, `max_pain` from the snapshot. These field names stay the same — only the data collector changes. The snapshot schema is stable.

### 5.5 TOTP Secret for Dhan

Dhan TOTP auth uses the same TOTP secret as the Dhan mobile app login.  
**Action needed:** Extract the TOTP secret from the Dhan 2FA setup and store in `.kite_secrets` equivalent.

The existing `kite_totp_auth.py` auto-renewal infrastructure can be cloned with minimal changes:
- Replace `kiteconnect` login flow with `POST /app/generateAccessToken`
- Same TOTP generation logic (pyotp library)
- Same systemd cron at 08:30 IST

---

## 6. Data We Don't Currently Use But Could

Dhan provides these that Kite doesn't — worth evaluating after migration:

### 6.1 Greeks in Real-Time

Delta, Theta, Gamma, Vega per strike, real-time from option chain.  
Current approach: ignore Greeks entirely in strategy decisions.

**Potential uses:**
- **Delta as direction confirmation**: If ATM delta is drifting from 0.5 toward 0.6 (calls gaining delta), underlying is trending up
- **Theta decay rate**: Know exactly how much time decay costs per bar — better P&L attribution
- **Vega for IV sensitivity**: Flag when IV is moving faster than expected (regime shift signal)

### 6.2 Option Chain OI Change per Bar

From expired options API + live chain: OI change per minute.  
Currently we compute `oi_change_30m` from the forward-collected data.  
With historical OI at 1-minute granularity, we can train on **OI velocity** (how fast OI is building/unwinding) — a stronger signal than absolute OI.

### 6.3 25-Minute Candles

Dhan supports 25-minute bars (Kite doesn't).  
Not currently needed but interesting for session-phase features.

---

## 7. Migration Architecture — Before and After

### Current (Kite-based):
```
Kite WebSocket → ingestion_app → Redis ticks
Kite REST (option chain) → ingestion_app → Redis option chain
                                          ↓
                                   snapshot_app (assembles 1-min snapshots)
                                          ↓
                                   MongoDB + strategy_app
```

### After Migration (Dhan-only):
```
Dhan WebSocket (binary) → DhanFeedAdapter → Redis ticks   (replaces KiteWS)
Dhan REST (optionchain) → ingestion_app → Redis option chain  (same structure)
                                         ↓
                                  snapshot_app (unchanged)   ← NO CHANGES HERE
                                         ↓
                                  MongoDB + strategy_app     ← NO CHANGES HERE
```

**Key principle:** Only `ingestion_app/` changes. `snapshot_app/`, `strategy_app/`, `execution_app/` are all unchanged.

---

## 8. Migration Phases

### Phase 1 — Token Automation (Day 1, ~2h)
- Implement `dhan_totp_auth.py` using `POST /app/generateAccessToken`
- Add TOTP secret to secrets store
- Test auto-renewal at 08:30 IST
- **Outcome:** No more manual Dhan token rotation

### Phase 2 — Live Feed Migration (Days 2–4, ~1 week dev)
- Build `DhanFeedAdapter` using `dhanhq.MarketFeed` SDK
- Subscribe: BankNifty fut, India VIX, ATM CE/PE via WebSocket
- Replace Kite option chain REST poll with Dhan `optionchain` endpoint
- Wire security ID lookup (download CSV daily, cache in Redis)
- Run Dhan and Kite in **parallel** for 2–3 live days to validate snapshots match
- **Outcome:** Kite ingestion replaced; snapshot quality same

### Phase 3 — Historical Data Backfill (Days 5–7, ~1 week)
- Write `dhan_backfill.py` to pull 5 years of:
  - BankNifty futures intraday (90-day chunks, ~20 calls)
  - India VIX intraday (same)
  - Expired options chain (30-day chunks, ~60 calls per expiry cycle)
- Merge with existing `snapshots_ml_flat_v2` parquet
- Build extended feature set (add delta, OI velocity)
- **Outcome:** 5-year training dataset vs current 4-year, with Greeks

### Phase 4 — Retrain on Extended Data (1 week on ML VM)
- Run full HPO sweep on 5-year dataset
- Label: 5-min 100pt magnitude (same as current)
- Add Greek features: delta, vega, OI velocity
- Validate: does 5-year model improve OOS AUC over current 0.6534?
- **Outcome:** Better entry model with more history + new features

---

## 9. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Dhan WebSocket binary parser bugs | Medium | High | Use official SDK first |
| Security ID lookup fails for new expiry | Low | High | Download instrument CSV every morning, cache |
| Option chain 3-sec rate limit causing lag | Very Low | Low | 60-sec poll interval = well within limit |
| Snapshot field mismatch causing model failures | Low | High | Run parallel Dhan+Kite for 3 live days |
| 5-year historical data gaps | Low | Medium | Check data completeness per expiry before training |
| Dhan token expiry during trading hours | Low | High | `/v2/RenewToken` + auto-retry on 401 |

---

## 10. Decision Points

**Q1: Use Dhan Python SDK or build from scratch?**  
Recommendation: Use SDK (`dhanhq` v2.1.0) for WebSocket. REST calls are simple enough to call directly. SDK adds minimal overhead.

**Q2: Migrate option chain to WebSocket or keep REST poll?**  
Recommendation: Keep REST poll for option chain (1 call per minute per expiry, all 50 strikes in one call). WebSocket is better for futures and VIX tick, but subscribing 50+ option strikes individually is unnecessary overhead.

**Q3: Extend existing parquet or build new dataset?**  
Recommendation: Build new standalone dataset (`snapshots_dhan_v1`) from Dhan historical APIs. Keep existing `snapshots_ml_flat_v2` for continuity. Train on Dhan data, validate it reproduces current model AUC before switching training pipeline.

**Q4: What to do about the Greeks?**  
Recommendation: Add as optional features in Phase 4. Don't change model architecture for Phase 1–3. Evaluate AUC improvement after backfill. If delta improves the model, keep it; otherwise ignore.

---

## 11. Immediate Action Items (Return from Break)

1. **Get Dhan TOTP secret** — the numeric TOTP seed from Dhan app's 2FA setup. Store in `/opt/option_trading/.kite_secrets` alongside Kite secrets.
2. **Subscribe to Dhan Data API** (₹499+tax/month) — needed for historical + expired options. Confirm if already subscribed (check account).
3. **Download instrument CSV** — `GET /v2/instrument/NSE_FNO` and `IDX_I` to find BankNifty security IDs.
4. **Test token generation** — confirm `POST /app/generateAccessToken` with TOTP works for account.
5. **Run `entry_allsession_full_v1` gate check on ML VM** — get AUC for the trained model before starting migration.

---

## Summary

| What changes | What stays the same |
|---|---|
| `ingestion_app/kite_*.py` → `ingestion_app/dhan_*.py` | `snapshot_app/` (unchanged) |
| WebSocket binary parser (SDK handles it) | `strategy_app/` (unchanged) |
| Token refresh logic (TOTP-based, automatable) | `execution_app/` (already Dhan) |
| Security ID lookup replacing Kite instrument tokens | All ML models (unchanged) |
| Training data source (Dhan APIs vs forward-collection) | Feature schema (same field names) |

**Biggest win:** 5-year expired options history with IV/OI/Greeks for ML retraining — this is something Kite has never offered and was the main bottleneck on our training data quality.

**Token renewal:** Fully automatable with TOTP — no manual step needed, same infrastructure as current Kite TOTP flow.

---

*Sources: [DhanHQ v2 Docs](https://dhanhq.co/docs/v2/), [Expired Options API](https://dhanhq.co/docs/v2/expired-options-data/), [Live Market Feed](https://dhanhq.co/docs/v2/live-market-feed/), [Option Chain](https://dhanhq.co/docs/v2/option-chain/), [Authentication](https://dhanhq.co/docs/v2/authentication/), [DhanHQ-py SDK](https://github.com/dhan-oss/DhanHQ-py)*
