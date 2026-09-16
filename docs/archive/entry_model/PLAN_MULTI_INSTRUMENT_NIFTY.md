# Multi-Instrument (NIFTY + BankNifty) — Team Execution Plan

**Last updated:** 2026-06-29  
**Branch:** `feat/dhan-feature-engine` (56 uncommitted files — see Track A)  
**Status:** BankNifty live (paper, Dhan). NIFTY models trained, SIM validated, live stack not yet deployed.

---

## What This Plan Is

A step-by-step track for any team member (human or AI agent) to:
1. Ship the current in-progress branch cleanly
2. Stand up a parallel NIFTY live stack (paper first)
3. Validate the real-money execution path via Dhan sandbox
4. Gate real-money trading on proven live-paper results

---

## Architecture (what runs in production)

```
ML VM (training only)              Runtime VM (option-trading-runtime-01, asia-south1-b)
──────────────────────             ────────────────────────────────────────────────────────
dhan_data_pipeline (build)         ingestion_app  → Dhan REST/WS feed (BROKER=dhan)
train_direction*.py                snapshot_app   → builds snapshots → Redis pub
multi_day_runner (SIM replay)      strategy_app   → consumes snapshots → signals → votes
                                   execution_app  → sends orders to Dhan NFO
                                   persistence_app→ MongoDB (strategy_positions, votes, traces)
                                   dashboard      → FastAPI UI + React (port 8002)
```

**Namespace design:** `STRATEGY_INSTRUMENT` env var scopes a whole container.
- BankNifty = primary (unsuffixed): `strategy_positions`, `market:snapshot:v1`, `/app/.run/strategy_app/`
- NIFTY = secondary (slug inserted): `strategy_positions_nifty`, `market:nifty:snapshot:v1`, `/app/.run/strategy_app_nifty/`
- Same Docker image for both — only env vars differ. Zero collision by design.

**Token management:** Dhan TOTP auto-refresh runs daily at 08:00 IST (pre-market) via systemd timer on the runtime VM. No manual tokens. Secrets in `/opt/option_trading/.env.totp` (chmod 600).

---

## State of Code (as of 2026-06-29)

| Component | Status | Location |
|-----------|--------|----------|
| Instrument namespace axis (kind × instrument) | ✅ done, NOT committed | `contracts_app/sim_namespace.py` |
| InstrumentSpec registry (lot/strike/cadence) | ✅ done, NOT committed | `contracts_app/instruments.py` |
| Topic scoping (STRATEGY_INSTRUMENT) | ✅ done, NOT committed | `contracts_app/topics.py` |
| Lot-size resolver (NIFTY=75, BankNifty=15/30) | ✅ done, NOT committed | `strategy_app/constants.resolve_lot_size()` |
| Cost-gate fix (spot-scaled reference move) | ✅ done, NOT committed | `strategy_app/engines/strategies/entry_cost_gate.py` |
| Ingestion BROKER=dhan switch | ✅ deployed (scp only) | `ingestion_app/runner.py`, `dhan_data_service.py`, `dhan_client.py` |
| Dhan option-chain fix (numeric scrip + IDX_I) | ✅ deployed (scp only) | `ingestion_app/dhan_client.py` |
| Dhan TOTP auto-refresh (systemd timer) | ✅ deployed | `/opt/option_trading/ops/gcp/dhan_token_refresh.sh` |
| Dashboard multi-instrument UI (S1–S6) | ✅ done, NOT committed | `market_data_dashboard/static/webapp/multi-instrument.jsx` |
| 5 new instrument-aware API endpoints | ✅ done, NOT committed | `market_data_dashboard/routes/{instruments,config,model_health,signals_ws,trades}_routes.py` |
| NIFTY entry model | ✅ on ML VM | `~/nifty_entry_bundle.joblib` (9 feat, kind=entry_only_bundle) |
| NIFTY direction model | ✅ on ML VM | `~/nifty_direction_v1.joblib` (AUC 0.64, kind=direction_only_bundle) |
| NIFTY runtime sim snapshots | ✅ on runtime VM | `~/dhan_pipeline_nifty_sim/` (19 days, full ATM±5 chain) |
| Regression suite | ⚠️ 4 failures | Likely order-dependent flakiness; verify before deploy |

---

## Track A — Stabilize & Commit (unblocks everything)

### A1: Resolve the 4 test failures

```bash
cd /c/code/option_trading/option_trading_repo
python -m pytest strategy_app/tests/ -q --tb=short -rf 2>&1 | grep -E "FAILED|passed|failed"
```

The 4 failures appeared at the 76% mark (`strategy_app/tests/`). Known causes of flakiness in this suite: env-var state leak between tests (STRATEGY_INSTRUMENT, STRATEGY_LOT_SIZE). Check by running in isolation:

```bash
python -m pytest <FAILED_TEST_PATH> -v
```

If they pass in isolation → mark as order-dependent flakiness (document in `conftest.py` with an env-reset fixture). If they fail in isolation → the regression is from the instrument-axis changes (most likely `resolve_lot_size()` picking up STRATEGY_INSTRUMENT from a prior test's env).

**Fix pattern for env-isolation failures:**
```python
@pytest.fixture(autouse=True)
def _clear_instrument_env(monkeypatch):
    monkeypatch.delenv("STRATEGY_INSTRUMENT", raising=False)
    monkeypatch.delenv("STRATEGY_LOT_SIZE", raising=False)
```

### A2: Commit the branch

```bash
cd /c/code/option_trading/option_trading_repo
git add contracts_app/ strategy_app/ execution_app/ ingestion_app/ \
        snapshot_app/ persistence_app/ market_data_dashboard/ \
        ml_pipeline_2/configs/research/staged_dual_recipe.entry_nifty_weekly_v1.json \
        docs/
git commit -m "feat(multi-instrument): instrument-axis namespace + NIFTY serving plane + Dhan ingestion

- contracts_app: Namespace gains instrument axis (BANKNIFTY=primary/unsuffixed, NIFTY=slug-suffixed)
  for collections, topics, run-dirs, state-keys, consumer-locks. BankNifty output byte-identical.
  InstrumentSpec registry (lot/strike/cadence). current_instrument() from STRATEGY_INSTRUMENT env.
- strategy_app: resolve_lot_size() (NIFTY=75, BN-legacy-15/30). Cost-gate fix: spot-scaled
  REF_MOVE_PT + instrument lot_qty so NIFTY ~24k is correctly evaluated (was blocking 100% of bars).
  Risk manager, risk calculator, position tracker, execution adapters all registry-driven.
- ingestion_app: BROKER=dhan branch in runner (skips Kite preflight/collectors). DhanDataService
  option-chain fix: numeric index scrip + IDX_I + expirylist + oc-dict parser + expiry injection.
- market_data_dashboard: 5 new instrument-aware endpoints + multi-instrument.jsx UI switcher.
- NIFTY SIM validated: 37 trades/14d, full lifecycle with NIFTY lot 75, zero crashes.

Co-Authored-By: Claude Sonnet 4.6 (1M context) <noreply@anthropic.com>"
```

### A3: Sync runtime VM from committed code

```bash
# On runtime VM (ssh option-trading-runtime-01)
cd /opt/option_trading
sudo git pull origin feat/dhan-feature-engine   # or main after merge
```

---

## Track B — NIFTY Live Stack

### B1: Copy NIFTY models to runtime VM

```bash
# From ML VM -> runtime VM (or local -> runtime)
gcloud compute scp option-trading-ml-01:/home/amits/nifty_entry_bundle.joblib \
    option-trading-runtime-01:/opt/option_trading/models/nifty_entry_bundle.joblib --zone=asia-south1-b
gcloud compute scp option-trading-ml-01:/home/amits/nifty_direction_v1.joblib \
    option-trading-runtime-01:/opt/option_trading/models/nifty_direction_v1.joblib --zone=asia-south1-b
```

Add to `.env.compose` on runtime VM:
```
NIFTY_ENTRY_ML_MODEL_PATH=/app/models/nifty_entry_bundle.joblib
NIFTY_DIRECTION_ML_MODEL_PATH=/app/models/nifty_direction_v1.joblib
NIFTY_ENTRY_ML_MIN_PROB=0.05
NIFTY_INSTRUMENT_SYMBOL=NIFTY26JULFUT
```

### B2: Define NIFTY compose services

Add to `docker-compose.gcp.yml` under the existing services — mirrors BankNifty services but with `STRATEGY_INSTRUMENT=NIFTY`:

```yaml
  ingestion_app_nifty:
    image: option_trading-ingestion_app          # same image as BankNifty
    environment:
      TZ: "Asia/Kolkata"
      REDIS_HOST: redis
      MONGO_HOST: mongo
      BROKER: dhan
      STRATEGY_INSTRUMENT: NIFTY
      INSTRUMENT_SYMBOL: "${NIFTY_INSTRUMENT_SYMBOL:-NIFTY26JULFUT}"
      DHAN_CLIENT_ID: "${DHAN_CLIENT_ID:-}"
      DHAN_ACCESS_TOKEN: "${DHAN_ACCESS_TOKEN:-}"
      LIVE_TOPIC: "market:nifty:snapshot:v1"
      SNAPSHOT_V1_TOPIC: "market:nifty:snapshot:v1"
    # ... (same network + depends_on as ingestion_app)

  snapshot_app_nifty:
    image: option_trading-snapshot_app
    environment:
      STRATEGY_INSTRUMENT: NIFTY
      LIVE_TOPIC: "market:nifty:snapshot:v1"
      MARKET_DATA_API_URL: "http://ingestion_app_nifty:8004"
    # ...

  strategy_app_nifty:
    image: option_trading-strategy_app
    environment:
      STRATEGY_INSTRUMENT: NIFTY
      ENTRY_ML_MODEL_PATH: "${NIFTY_ENTRY_ML_MODEL_PATH:-}"
      DIRECTION_ML_MODEL_PATH: "${NIFTY_DIRECTION_ML_MODEL_PATH:-}"
      ENTRY_ML_MIN_PROB: "${NIFTY_ENTRY_ML_MIN_PROB:-0.05}"
      STRATEGY_LOT_SIZE: "75"
      ML_ENTRY_DIRECTION_MODE: "direction_ml"
      ROLLOUT_STAGE: "paper"
      SNAPSHOT_V1_TOPIC: "market:nifty:snapshot:v1"
      STRATEGY_INSTRUMENT: NIFTY
    # ...

  persistence_app_nifty:
    image: option_trading-strategy_persistence_app
    environment:
      STRATEGY_INSTRUMENT: NIFTY
    # ...
```

### B3: Rebuild images + launch NIFTY stack

```bash
cd /opt/option_trading
# Rebuild strategy_app + snapshot_app with instrument-aware code (one build, both instruments use it)
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
    build strategy_app snapshot_app

# Launch NIFTY parallel stack (BankNifty stays on its existing running containers)
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
    up -d ingestion_app_nifty snapshot_app_nifty strategy_app_nifty persistence_app_nifty
```

### B4: Verify

```bash
# Snapshots publishing for NIFTY
docker logs option_trading-snapshot_app_nifty-1 | grep "health published" | tail -3

# Strategy traces in the _nifty collection
mongosh trading_ai --eval "db.strategy_decision_traces_nifty.find().sort({_id:-1}).limit(1).pretty()"

# Dashboard: open the UI -> instrument switcher should show NIFTY tile with live data
```

---

## Track C — Execution Sandbox Validation

**Purpose:** Prove the real order path (place/modify/cancel, lot validation, security-id resolution) before risking any real capital.

```bash
# On runtime VM: point execution_app at Dhan sandbox
docker exec option_trading-execution_app-1 env | grep DHAN_API_BASE
# Set temporarily: DHAN_API_BASE=https://sandbox.dhan.co/v2
# (or restart execution_app with this env var)

# Then trigger a paper signal through the system and verify the execution adapter
# calls sandbox.dhan.co instead of api.dhan.co
```

Repeatable adapter smoke (runs fail-closed unless `DHAN_API_BASE=https://sandbox.dhan.co/v2`):

```bash
docker exec \
  -e STRATEGY_INSTRUMENT=NIFTY \
  -e DHAN_API_BASE=https://sandbox.dhan.co/v2 \
  option_trading-execution_app-1 \
  python ops/gcp/dhan_sandbox_smoke.py \
    --expiry YYYY-MM-DD \
    --strike 24000 \
    --direction CE
```

Expected JSON evidence:
- `dhan_api_base` is `https://sandbox.dhan.co/v2`
- `instrument` is `NIFTY`
- `quantity` is `75` for one lot
- `security_id` is resolved from Dhan scrip master
- `place.error` captures Dhan sandbox rejects such as `DH-905`/`DH-901`

Key things to verify:
- NIFTY option order: `quantity = 1 lot × 75 = 75` (not 30 or 15)
- Security-id resolved from Dhan scrip master (not hardcoded)
- `DH-905`/`DH-901` error handling works
- Order response captured in `execution_fills`

---

## Track D — TOTP / Token Robustness

**D1: Store TOTP on ML VM** (so Dhan data fetches auto-refresh):
```bash
# Copy .env.totp to ML VM
gcloud compute scp option-trading-runtime-01:/opt/option_trading/.env.totp \
    option-trading-ml-01:/home/amits/.env.totp --zone=asia-south1-b
```

**D2: Token refresh manual trigger** (when needed between auto-refresh cycles):
```bash
sudo /opt/option_trading/ops/gcp/dhan_token_refresh.sh
```

---

## Real-Money Gate (do NOT skip this)

Both BankNifty and NIFTY must independently satisfy:
1. **Positive net P&L** over ≥ 5 live-paper sessions (real Dhan fills, paper capital)
2. **Execution fills verified**: real order IDs in `execution_fills` (not `paper_*` prefixes)
3. **Cost model checked**: slippage measured from actual fills vs. estimated
4. **No simulation contamination**: all signal_ids from `live-*` run, not `sim-*`

Current status:
- BankNifty: no net-positive live session on record
- NIFTY: SIM showed −0.17% (PF 0.81) — negative expectancy, real money off

---

## Key Files for New Agents

| File | What it tells you |
|------|-------------------|
| `docs/ARCHITECTURE.md` | System component overview |
| `docs/SYSTEM_FLOW.md` | Data flow diagrams |
| `docs/RUNTIME_STATE_AND_RECOVERY.md` | Recovery procedures |
| `docs/GO_LIVE_CHECKLIST.md` | Pre-flight before real money |
| `docs/ENGINE_DECISION_FLOW.md` | How the strategy engine evaluates each bar |
| `contracts_app/sim_namespace.py` | Namespace contract (instrument × kind) — read this first |
| `contracts_app/instruments.py` | InstrumentSpec registry (lot_size, strike_step, index_security_id) |
| `strategy_app/constants.py` | `resolve_lot_size()` — the one lot-size source |
| `ingestion_app/api_service.py` | `_resolve_broker()` — Kite vs Dhan switch |
| `.env.compose` on runtime VM | Live config source of truth |
| `/opt/option_trading/.env.totp` | TOTP secrets (runtime VM, chmod 600, never in repo) |

---

## Key Invariants (don't break these)

1. **BankNifty primary = unsuffixed.** All existing collections, topics, and run-dirs must stay unchanged for BankNifty. Verified by `TestInstrumentParity` in `tests/test_sim_namespace.py`.
2. **`resolve_lot_size()` is the one lot-size source.** Never hardcode `30`, `15`, or `75` in new strategy/execution code.
3. **Cost gate is instrument-aware.** `entry_cost_gate.py` scales `REF_MOVE_PT` by `spot / 52000`. Do not revert this — it blocked 100% of NIFTY entries when BankNifty-calibrated.
4. **Token never in the repo.** `DHAN_ACCESS_TOKEN` lives in `.env.compose` (gitignored). TOTP secrets in `.env.totp` (also gitignored, chmod 600).
5. **Paper before real.** `ROLLOUT_STAGE=paper` on all new stacks until the real-money gate is passed.
