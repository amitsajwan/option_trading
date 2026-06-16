# Dhan Full Migration Plan
**Trigger:** When Kite API subscription expires  
**Goal:** Replace Kite as ingestion + data source; keep Dhan execution as-is; unlock 5-year option history for ML training

---

## Current State (as of 2026-06-04)

| Layer | What runs it | Notes |
|---|---|---|
| Live tick ingestion | Kite websocket | `ingestion_app/` — ltp, depth, option chain |
| Snapshot assembly | `snapshot_app/` | Consumes Kite ticks via Redis |
| ML training data | 2020–2024 parquet | `options/`, `snapshots_ml_flat_v2/`, 621 MB |
| Execution | **Dhan** (`EXECUTION_ADAPTER=dhan`) | Already live, verified ₹10k balance |
| Daily TOTP refresh | systemd cron 08:30 IST | Kite-specific; brittle (see `project_totp_refresh_hardening`) |

**Pain points with Kite that Dhan eliminates:**
- Daily TOTP token refresh (manual/semi-automated)  
- Depth-instrument rotation every Thu (memory `project_gcp_live_mode`)  
- No historical option-chain data (the ML training ceiling)  

---

## What Dhan Data API Unlocks (₹499+tax/month)

Verified blocked today (`DH-902`), unblocked after subscription:

| Endpoint | What it gives | ML/Trading value |
|---|---|---|
| `POST /v2/charts/rollingoption` | 5yr, 1-min OHLC+OI+IV per ATM±n strike | **Replaces entire Kite forward-collection bottleneck** |
| `POST /v2/charts/intraday` | 5yr, 1-min index OHLC (BANKNIFTY spot) | Supplement/replace Kite historical |
| `POST /v2/optionchain` | Live chain: greeks (Δ/Θ/Γ/V), IV, OI, bid/ask per strike | Better than assembled Kite snapshot |
| `POST /v2/optionchain/expirylist` | All expiries for an underlying | Needed for backfill pagination |
| Websocket `FULL` packet | LTP + Quote + OI + depth in one packet | Drop-in for Kite websocket ticks |

**Key constraint confirmed from scrip master:**  
`securityId=25` = BANKNIFTY index (`IDX_I` segment)  
Token expires daily — but Dhan token lifetimes are configurable up to 30 days (no TOTP needed).

---

## Migration Phases

### Phase 0 — Subscribe & Verify (Day 1, ~30 min)
_Do this the day Kite expires or earlier._

1. Subscribe Data API on `web.dhan.co → Profile → DhanHQ Trading APIs`
2. Run the verification probe (already written, no code change):
   ```bash
   # on VM
   cd /opt/option_trading
   source .env.compose  # picks up DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN
   python probe_dhan_history.py --security-id 25 --segment IDX_I
   ```
3. **Pass criteria before proceeding:**
   - `rollingoption` returns candles going back to at least 2021
   - `median_gap == 60s` (true 1-minute)
   - `iv` and `oi` arrays are non-empty
   - `spot` field present (needed to reconstruct absolute strikes)
   - Strike offsets ATM±0 through ATM±12 served (matches our OTM depth)

**If any criterion fails → do not start Phase 2 backfill; investigate first.**

---

### Phase 1 — Fidelity Cross-check (Day 1–2, ~2 hrs)
_Confirm Dhan historical matches what we've been training on._

Write `probe_dhan_kite_diff.py`:
- Pick 3 recent forward-collected days that exist in both Kite snapshots (JSONL) and Dhan history
- For each day: compare per-minute `close`, `iv`, `oi` for ATM CE and PE
- Accept if: median absolute diff `close < 0.5%`, `iv < 1 vol pt`, `oi < 2%`

This guards against silent definition differences (e.g. Dhan IV = Black-Scholes vs Kite IV = exchange-reported).

---

### Phase 2 — ML Training Data Backfill (Day 2–5)
_Replace the 2020–2024 parquet + add 2025–2026 fresh OOS data._

**Write `dhan_options_backfill.py`** targeting existing parquet schema:
```
timestamp, trade_date, symbol, open, high, low, close, volume, oi, strike, option_type, expiry_str, iv
```

Backfill strategy:
1. **2025–2026 first** (highest value — completely absent today, true OOS for trained models)
2. **2024 overlap** — cross-validate vs existing Kite parquet, replace if Phase 1 passes
3. **2021–2023** — rebuild from Dhan if existing parquet has gaps or quality issues
4. **2020** — Dhan 5yr window may not reach back to Jan 2020; keep existing Kite data if needed

Pagination: 30 days/call × strike offsets (ATM-12 to ATM+12 = 25 offsets) × 2 types (CE/PE) = ~50 calls per 30-day window. At 3s rate limit = ~150s per month of data. **Full 5yr ≈ 90 min of API time.**

Output: same `parquet_data/options/year=YYYY/month=MM/data.parquet` layout — zero ML pipeline changes needed.

---

### Phase 3 — Live Ingestion Switch (Day 3–7)
_Replace Kite websocket with Dhan websocket/REST for live tick ingestion._

**Files to change:**
- `ingestion_app/collectors/websocket_tick_collector.py` → add `DhanTickCollector` class
- `ingestion_app/collectors/ltp_collector.py` → swap to Dhan Market Quote API (1000 instruments/call)
- `ingestion_app/collectors/depth_collector.py` → use Dhan `FULL` websocket packet (OI + depth together, no Thursday rotation)
- `ingestion_app/kite_client.py` → keep for fallback; add `dhan_client.py`
- `ingestion_app/token_refresh.py` → replace Kite TOTP logic with Dhan token renewal (30-day tokens, no TOTP)
- `docker-compose.yml` → add `DHAN_INGESTION=1` env flag; ingestion_app selects client based on flag

**Snapshot app (`snapshot_app/`):** No changes needed — it consumes normalized Redis messages; the tick format normalization lives in ingestion_app collectors.

**Gate: run Dhan ingestion in parallel with Kite for 2–3 live days** before cutting over. Compare snapshot field coverage.

---

### Phase 4 — Retrain ML Models on Extended Data (Day 5–10)
_First genuine multi-year OOS test._

- Re-run `ml_pipeline_2` campaign with 2020–2025 train / 2026 OOS split
- Previously every "OOS" test was Aug–Oct 2024 (within the training window or barely outside)
- 2025–2026 is data models have **never** seen in any form — real OOS
- Compare: current models on 2020–2024 vs retrained on 2020–2025 held-out on 2026
- Key metric: does bootstrap CI for PF lower bound stay > 1.0 on 2026 data?

---

### Phase 5 — Decommission Kite (Day 7–14)
Once Phase 3 ingestion is stable for a full week:
- Remove `ingestion_app/kite_auth.py`, `kite_client.py`, `kite_totp_auth.py`
- Remove `ingestion_app/token_refresh.py` (Kite variant)
- Remove Kite creds from `.env.compose` and VM secrets
- Remove systemd `kite-token-refresh` cron unit
- Archive raw Kite JSONL snapshots (keep, do not delete — they are the training baseline)

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Dhan `rollingoption` IV definition differs from Kite | Medium | Phase 1 cross-check gates Phase 2 |
| ATM±12 depth not served (doc says ATM±3 for non-expiry weeks) | High | Test in Phase 0 probe; may need to stitch weekly+monthly windows |
| Dhan token expires mid-day (current token: daily) | Medium | Switch to 30-day token at same time as Data API subscription |
| `rollingoption` doesn't reach back to 2020 | Medium | Keep existing 2020–2022 Kite parquet as-is; backfill only 2023+ |
| Live ingestion gap during cutover | Low | Run both in parallel (Phase 3 gate) before switching |

---

## Files Already Written / Ready

| File | Status |
|---|---|
| `execution_app/adapter/dhan.py` | Live, verified working |
| `probe_dhan_history.py` | Written, run after Data API subscription |
| `probe_dhan_kite_diff.py` | To write in Phase 1 |
| `dhan_options_backfill.py` | To write in Phase 2 |
| `ingestion_app/collectors/dhan_tick_collector.py` | To write in Phase 3 |

---

## Summary Timeline

```
Day Kite expires
  └─ Phase 0: Subscribe + run probe_dhan_history.py         (~30 min)
  └─ Phase 1: Fidelity cross-check (Dhan vs Kite overlap)   (~2 hrs)
Day +1
  └─ Phase 2: Backfill 2025-2026 options parquet            (~2 hrs code + 90 min API)
Day +2
  └─ Phase 3: Wire up Dhan live ingestion, parallel run
Day +3 to +7
  └─ Phase 3 cont: Monitor parallel, cut over
  └─ Phase 4: Retrain ML on 2020-2025, validate on 2026
Day +7 to +14
  └─ Phase 5: Decommission Kite code + creds
```
