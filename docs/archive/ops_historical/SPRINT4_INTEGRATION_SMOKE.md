# Sprint 4 — Live enrichment integration smoke test (D2-S4)

**Story:** D2-S4 (P1, 2 pts) — see [SCRUM_BOARD_ML_ENTRY_DIRECTION.md](../SCRUM_BOARD_ML_ENTRY_DIRECTION.md#d2-s4--integration-smoke-test-for-live-enrichment-rollout--backlog--p1--2-pts)
**Scope:** Prove the end-to-end path `ingestion_app -> snapshot_app -> Mongo -> downstream consumers` is intact after Sprint 4 enrichment work (D2-S1/S2/S3 cross-asset + block-flow, D3-S4 anchored VWAP). This is a runbook; **results** of each run go in `docs/audits/SPRINT4_INTEGRATION_SMOKE_<YYYY-MM-DD>.md`.

Pre-reqs: market hours (09:15-15:30 IST) for steps 1-4, post-09:20 IST on day N+1 for step 3 only. VM ops pattern per [reference_gcp_vm_access.md](../../memory/reference_gcp_vm_access.md).

---

## 0. Env wiring

The cross-asset path in [snapshot_app/core/market_snapshot.py:1777-1832](../../snapshot_app/core/market_snapshot.py#L1777-L1832) is gated on three env vars. They are wired into `snapshot_app` in [docker-compose.yml](../../docker-compose.yml) — verify they're set in `.env.compose`:

```
CROSS_ASSET_ENABLED=1
NIFTY_FUT_SYMBOL=NFO:NIFTY26JUNFUT
BLOCK_TRADE_MIN_LOTS=5
```

Verify the values landed inside the container:

```powershell
# Local
docker compose --env-file .env.compose exec snapshot_app printenv CROSS_ASSET_ENABLED NIFTY_FUT_SYMBOL BLOCK_TRADE_MIN_LOTS
```

```bash
# VM
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading \
  --command "cd ~/option_trading_repo && docker compose --env-file .env.compose exec -T snapshot_app printenv CROSS_ASSET_ENABLED NIFTY_FUT_SYMBOL BLOCK_TRADE_MIN_LOTS"
```

**PASS:** three non-empty lines, `CROSS_ASSET_ENABLED=1`. **FAIL:** empty value or `0` — test is invalid, stop.

---

## 1. Tick contract (ingestion_app)

Endpoint: [ingestion_app/api_service.py:616](../../ingestion_app/api_service.py#L616) — `GET /api/v1/market/tick/{instrument}`. Required fields built at lines 268-289: `last_quantity`, `best_bid`, `best_ask`, `mid`.

```powershell
# Local
$base = "http://localhost:8004/api/v1/market/tick"
$syms = @(
  "NSE:NIFTY 50",
  "NSE:NIFTY BANK",
  "NFO:NIFTY26JUNFUT",
  "NFO:BANKNIFTY26JUNFUT",
  "NFO:BANKNIFTY26JUN52000CE",
  "NFO:BANKNIFTY26JUN52000PE"
)
foreach ($i in $syms) {
  $enc = [uri]::EscapeDataString($i)
  Write-Host "=== $i ==="
  curl.exe -s "$base/$enc" | python -m json.tool
}
```

For VM runs, port-forward first:

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading -- -L 8004:localhost:8004
# Then run the same loop from your laptop against localhost:8004.
```

**PASS:** every response has non-null `last_quantity`, `best_bid`, `best_ask`, `mid`.
**FAIL:** any one null on any symbol — block-flow + cross-asset features cannot fill; stop, file an ingestion ticket.

ATM strike of the day: take spot from the NIFTY BANK cash tick, round to nearest 100, substitute into the CE/PE symbol.

---

## 2. Snapshot persistence (Mongo)

Let `snapshot_app` run 5-10 minutes after market open. Sample the two most recent docs:

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "
docker exec option_trading-dashboard-1 mongosh trading_ai --quiet --eval '
db.phase1_market_snapshots.find({}, {
  \"payload.snapshot.nifty_context\":1,
  \"payload.snapshot.underlying_context\":1,
  \"payload.snapshot.block_flow\":1,
  \"payload.snapshot.futures_derived.vwap_anchored_open\":1,
  \"payload.snapshot.futures_derived.price_vs_vwap_anchored\":1,
  trade_date:1, snapshot_ts:1
}).sort({snapshot_ts:-1}).limit(2).pretty()'
"
```

**PASS criteria:**

- `nifty_context`, `underlying_context`, `block_flow` keys present (values may be `null` for the first 1-3 minutes of warm-up — expected for the rolling-window aggregator at [snapshot_app/core/market_snapshot.py:911-917](../../snapshot_app/core/market_snapshot.py#L911-L917)).
- `futures_derived.vwap_anchored_open` non-null from bar 2 onward.
- `futures_derived.price_vs_vwap_anchored` non-null from bar 2 onward.

Populated-doc counts over the last 30 minutes:

```bash
docker exec option_trading-dashboard-1 mongosh trading_ai --quiet --eval '
const since = new Date(Date.now() - 30*60*1000);
print("with nifty_context:", db.phase1_market_snapshots.countDocuments({snapshot_ts:{$gte:since}, "payload.snapshot.nifty_context":{$ne:null}}));
print("with block_flow:",    db.phase1_market_snapshots.countDocuments({snapshot_ts:{$gte:since}, "payload.snapshot.block_flow":{$ne:null}}));
print("with vwap_anch:",     db.phase1_market_snapshots.countDocuments({snapshot_ts:{$gte:since}, "payload.snapshot.futures_derived.vwap_anchored_open":{$ne:null}}));
print("total:",              db.phase1_market_snapshots.countDocuments({snapshot_ts:{$gte:since}}));
'
```

**PASS:** populated counts >= 80% of total (allowing for warm-up bars).

---

## 3. Session reset (day N+1)

Run after 09:20 IST on the day **after** snapshot_app has been collecting:

```bash
docker exec option_trading-dashboard-1 mongosh trading_ai --quiet --eval '
const today = new Date(new Date().toISOString().slice(0,10));
db.phase1_market_snapshots.find(
  {snapshot_ts:{$gte:today}},
  {snapshot_ts:1,
   "payload.snapshot.futures_derived.vwap_anchored_open":1,
   "payload.snapshot.futures_derived.last_price":1,
   "payload.snapshot.block_flow":1}
).sort({snapshot_ts:1}).limit(5).pretty()'
```

**PASS:**
- First doc of the day: `vwap_anchored_open` is null (warm-up) or within ~0.5% of `last_price` — not yesterday's close.
- `block_flow.rolling_*` counts start small (single-digits) rather than carrying yesterday's totals.

**FAIL:** anchored VWAP carries yesterday's value — file a session-reset bug against [snapshot_app/core/market_snapshot.py:1134-1137](../../snapshot_app/core/market_snapshot.py#L1134-L1137).

---

## 4. Downstream non-breakage

### 4a. Consumer logs

```powershell
# Local
docker compose --env-file .env.compose logs --since 15m persistence_app strategy_persistence_app | Select-String -Pattern "ERROR|TRACEBACK|KeyError|TypeError"
```

```bash
# VM
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "
cd ~/option_trading_repo && docker compose --env-file .env.compose logs --since 15m persistence_app strategy_persistence_app 2>&1 | grep -E 'ERROR|TRACEBACK|KeyError|TypeError' | tail -30"
```

**PASS:** zero new errors referencing `nifty_context`, `underlying_context`, `block_flow`, or `futures_derived`. Pre-existing unrelated errors are OK; note them in the audit doc.

### 4b. Dashboard health

```powershell
curl.exe -s http://localhost:8086/health  # local
```

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "curl -s http://localhost:8086/health"
```

### 4c. Snapshot builder unit tests

```powershell
python -m pytest snapshot_app/tests -k "snapshot or cross_asset or block" -x -q
```

**PASS:** green.

---

## 5. Audit scripts

### 5a. VIX field audit (R1S unblocker)

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "
cd ~/option_trading_repo && docker compose --env-file .env.compose run --rm strategy_app python ops/gcp/audit_vix_field.py"
```

**PASS:** `snapshot.vix` populated on IS quarters per [project_r1s_spec_2026-05-26.md](../../memory/project_r1s_spec_2026-05-26.md).

### 5b. Direction audit (known schema drift)

```bash
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "
cd ~/option_trading_repo && bash ops/gcp/run_direction_audit.sh 2026-05-26 2026-05-26"
```

**Expected outcome:** several feature names report `NO_DATA` because [docs/audits/direction_audit_template.py](../audits/direction_audit_template.py) does not line up with the v3 snapshot field names from D2-S1/S2/S3 + D3-S4. **Do not retry to green.** The mismatch IS the finding — file a follow-up "D1-S1.fix: align direction_audit_template.py with snapshot v3 schema" and proceed.

---

## 6. Document & sign off

Create `docs/audits/SPRINT4_INTEGRATION_SMOKE_<YYYY-MM-DD>.md` with one section per step above, each marked `PASS` / `FAIL` / `WARMUP`, attaching:

- the printenv output (step 0)
- one full tick payload per symbol (step 1)
- the two snapshot samples + the count summary (step 2)
- audit-script outputs (step 5)
- any follow-up ticket IDs filed

**Acceptance for D2-S4 signoff:**

- Steps 0, 1, 2, 4 PASS in at least one environment (local OR VM).
- Step 3 PASS confirmed within the next 2 trading days.
- Step 5b's schema drift converted to a tracked ticket — not silently ignored.
