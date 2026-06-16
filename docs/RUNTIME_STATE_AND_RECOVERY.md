# Runtime State, Best Config & Recovery — 2026-06-10

> **Why this exists:** the runtime VM `option-trading-runtime-01` was accidentally
> deleted (2026-06-10). This captures the full state so the live stack can be
> rebuilt from scratch, plus the strategic findings so no knowledge is lost.
> **Real money was OFF (paper) — zero financial loss.**

---

## 0. TL;DR
- **Project:** `amit-trading` (the old `algo-trading-496203` is DEAD — never use).
- **Best config:** balanced **entry (110pt / `020` model)** + **consensus direction** + **regime guard 0.010** + **scalper exits**, **paper, 1 lot**. Best P&L = **−0.40%** (not profitable — direction is the cap).
- **The bottleneck is DIRECTION (~57%)**, not entry (82%) or exits (optimal). The fix = **retrain direction on microstructure**, stack on the validated **57% fade**. Handover: `docs/ML_RETRAIN_HANDOVER.md`.
- **Don't rush the runtime rebuild** — nothing profitable runs on it. Rebuild when the retrain yields a model worth deploying.

---

## 1. What we have / what's safe / what's lost

| Asset | Where | Status |
|---|---|---|
| Engine + ops code | git `feat/intelligent-brain` (pushed) | ✅ safe |
| 2020–2024 training parquet | `gs://amit-trading-option-trading-snapshots/ml_pipeline/parquet_data/` | ✅ safe |
| Published models (entry/direction, all versions) | `gs://amit-trading-option-trading-models/published_models/` | ✅ safe |
| Runtime config bundle | `gs://amit-trading-option-trading-runtime-config/runtime` | ✅ (if last publish ran) |
| Mongo dump (live data **up to Jun 3**) | `gs://amit-trading-option-trading-snapshots/migration/mongo_full_20260603.archive.gz` | ✅ recoverable |
| **Live data Jun 4 → Jun 10** | only on deleted VM disk | ❌ **lost** (low impact — findings banked) |
| ML retrain VM `option-trading-ml-01` | `amit-trading`, RUNNING | ✅ up, untouched |
| All findings | this repo's `docs/` + Claude memory | ✅ safe |

---

## 2. Best-known findings (the strategic state)

| Component | Verdict | Detail |
|---|---|---|
| **Entry** | ✅ **strong, solved** | AUC 0.83, 4.37× move discrimination. Best label = **110pt / 0.20% (`020`)** — clears cost. ECE 0.009 (well-calibrated). |
| **Direction** | ❌ **DEAD — not 57%, it's 50%** | 2024: 50.3% quorum (coin flip) over 37,050 move-bars. 2026 OOS: **43.9% (INVERTS)**. The "57%" figure was from a contaminated, momentum-polluted measurement. All 6 independent tests converge: direction is not predictable. Order-flow proxies also ~50% at 1–5 bars. |
| **Exits** | ✅ **adaptive is best** | adaptive +0.62%, scalper −0.89%, lottery H2-negative. adaptive routes BREAKOUT→lottery (20% stop) — see gotcha below. |
| **P&L (buy-side)** | ❌ **−EV, no path to fix** | 125 live trades, zero net-positive days. The direction wall cannot be cleared — all proxies tested, all coin-flips. |
| **Costs** | real ~₹180–220/lot | Round-trip from 37 measured trades; ~1% of entry notional for ATM options. |
| **Refuted (don't re-chase)** | — | lottery exit (outlier-driven), buy-side direction signals, order-flow pressure, LLM direction overlay (follows vwap 79%, zero independent value), option depth (block_flow null). |
| **S3 Seller** | ✅ **ONLY ROBUST PATH** | 78% win, +₹1,692/trade, +₹123k 2024 paper. Drop-top3 +₹104k. Real money OFF until live-cycle paper. |

**Updated conclusion (2026-06-14):** There is no buy-side edge and no direction signal remaining to test. Go **sell-side** (S3 seller). See `docs/FINDINGS_2026-06-14.md` for full evidence.

---

## 3. BEST CONFIGURATION (engine env vars)

The live engine is the **deterministic** engine, profile **`trader_master_live_v1`**, in **PAPER**. Critical `.env.compose` values (the full file is in the GCS runtime bundle; these are the ones that matter + the gotchas):

```bash
# --- mode / safety ---
EXECUTION_ADAPTER=paper                 # REAL MONEY OFF. Set 'dhan' only after a config proves net-positive.
STRATEGY_ROLLOUT_STAGE=paper            # cosmetic; dhan adapter still places real orders if EXECUTION_ADAPTER=dhan
STRATEGY_PROFILE_ID=trader_master_live_v1
RISK_MAX_LOTS_PER_TRADE=1
RISK_LIVE_MIN_GRADE=OK
RISK_CAPITAL_ALLOCATED=41000
STRATEGY_CAPITAL_ALLOCATED=41000
RISK_MAX_CONSECUTIVE_LOSSES=6

# --- ENTRY (the balanced 110pt model) ---
ENTRY_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib   # = the 020/110pt model (AUC 0.83)
ENTRY_ML_MIN_PROB=0.25
STRATEGY_MIN_CONFIDENCE=0.80            # effective entry gate
ENTRY_TIME_WINDOWS=09:45-14:30          # IST

# --- DIRECTION (consensus; ~57-58%, the weak link) ---
ML_ENTRY_DIRECTION_MODE=consensus
DIRECTION_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib
REGIME_GUARD_MAX_ORW=0.010              # skip wide-opening-range (expansion) days — marginal help

# --- EXITS (scalper — optimal, do not touch) ---
EXIT_STRATEGY_MODE=scalper
EXIT_POLICY_STACK_ENABLED=1
EXIT_SCALPER_HARD_STOP_PCT=0.07
EXIT_MAX_LOSS_PCT=0.10                  # universal floor (wraps all modes — the hardstop-disaster fix)
EXIT_PREMIUM_TARGET_PCT=0.03
EXIT_TRAILING_ACTIVATION_PCT=0.015
EXIT_TRAILING_TRAIL_PCT=0.008
EXIT_THESIS_FAIL_BARS=5

# --- strike selection ---
STRATEGY_SMART_STRIKE_ENABLED=1
STRATEGY_STRIKE_MAX_OTM_STEPS=12
SMART_STRIKE_MAX_PREMIUM=1300

# --- feed (UPDATE the instrument monthly on futures roll!) ---
INSTRUMENT_SYMBOL=BANKNIFTY26JUNFUT     # rolls monthly — was 28MAY→26JUN; update on roll
NIFTY_FUT_SYMBOL=NFO:NIFTY26JUNFUT
INGESTION_COLLECTORS_ENABLED=1          # REQUIRED for the feed (was a silent-break source)
DEPTH_FEED_ENABLED=1
# DEPTH_FEED_INSTRUMENTS=...54200..54600 CE/PE for the June band — roll with the strikes
```

### Gotchas (hard-won this session)
1. **A new env var must be in the `docker-compose.yml` `environment:` block (3 services) to reach the container** — `.env.compose` alone is NOT enough (bit us on `REGIME_GUARD_MAX_ORW`, `DIRECTION_ML_CONFIDENCE_MIN`).
2. **`.env.compose` has a non-UTF-8 byte** — edit with `encoding='latin-1'` in Python.
3. **Two `.env.compose` files historically existed** — the canonical one is `/opt/option_trading/.env.compose` (the brain config). The `.deploy/runtime-config/` one reverts to DANGEROUS defaults.
4. **`15m_060pct` entry model = 0 trades** (too strict / feature gap). Keep `ENTRY_ML_MODEL_PATH` on `published/` (the 020).
5. **Direction for `trader_master_live_v1` comes from `ml_entry.py:_resolve_direction`, NOT `resolve_direction_consensus`** (that's dead code for this profile — see `docs/ENGINE_DECISION_FLOW.md`).

---

## 4. How to REBUILD the runtime VM + start everything

```bash
# 1. Recreate the VM (use the training template, or a fresh e2-standard-4)
gcloud compute instances create option-trading-runtime-01 \
  --project=amit-trading --zone=asia-south1-b \
  --machine-type=e2-standard-4 --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=100GB

# 2. SSH in, install docker + clone the repo to /opt/option_trading
gcloud compute ssh option-trading-runtime-01 --project=amit-trading --zone=asia-south1-b
#   sudo git clone <repo> /opt/option_trading && cd /opt/option_trading
#   git checkout feat/intelligent-brain   (or main, once merged)

# 3. Restore .env.compose (from GCS runtime bundle), or recreate from §3 above
#   gcloud storage cp gs://amit-trading-option-trading-runtime-config/runtime/.env.compose /opt/option_trading/.env.compose
#   (then re-apply the §3 best-config values, esp. EXECUTION_ADAPTER=paper + the 020 entry path)

# 4. Pull published models from GCS into the container's artifact path
#   gcloud storage rsync -r gs://amit-trading-option-trading-models/published_models \
#     /opt/option_trading/ml_pipeline_2/artifacts/published_models

# 5. (Optional) restore mongo live data up to Jun 3
#   gcloud storage cp gs://amit-trading-option-trading-snapshots/migration/mongo_full_20260603.archive.gz /tmp/
#   sudo docker exec -i option_trading-dashboard-1 mongorestore --gzip --archive < /tmp/mongo_full_20260603.archive.gz

# 6. Add fresh creds (1-day tokens):
#   - Kite: TOTP refresh (ops/gcp/kite_token_refresh.sh) + .env.totp
#   - Dhan: paste fresh DHAN_ACCESS_TOKEN (only matters if EXECUTION_ADAPTER=dhan)

# 7. Start the stack
cd /opt/option_trading && sudo docker compose --env-file .env.compose up -d

# 8. Verify (see §5)
```

### Daily-sync to prevent future loss (ADD on rebuild)
Set up a daily `mongodump` → `gs://amit-trading-option-trading-snapshots/migration/` cron so a VM loss never costs more than a day. (This is why Jun 4-10 was lost — sync only ran at the Jun-3 migration.)

---

## 5. Verify a healthy start
```bash
# engine on the right config:
sudo docker logs option_trading-strategy_app-1 2>&1 | grep -E 'starting engine|exit policy mode|ml_entry: loaded'
#   expect: engine=deterministic, rollout_stage=paper, profile=trader_master_live_v1,
#           exit policy mode=scalper, entry model path .../published/entry_only_model.joblib
# real money OFF:
sudo docker logs option_trading-execution_app-1 2>&1 | grep -E 'PaperAdapter|DhanAdapter'   # want PaperAdapter
# feed alive (during market hours):
sudo docker logs option_trading-ingestion_app-1 2>&1 | grep -iE 'preflight passed|TokenException'
# unhalted:
sudo docker exec option_trading-strategy_app-1 sh -c 'ls .run/strategy_app/*halt* 2>/dev/null || echo UNHALTED'
```
VM-access pattern: `gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=amit-trading --command "..."`. Mongo + dashboard sims run **inside** `option_trading-dashboard-1`.

---

## 6. Code: finalize + merge to main?

- **Branch:** `feat/intelligent-brain` — all of today's work is committed + pushed.
- **Key commits:** regime-guard relocation to the live path (`787edc2`), `ENGINE_DECISION_FLOW.md` (authoritative engine map), `ML_RETRAIN_HANDOVER.md`, GCP project fix to `amit-trading`, CLEANUP-BACKLOG markers, this doc.
- **Recommendation: YES, merge to `main`** after a quick review. What's worth keeping:
  - ✅ `ENGINE_DECISION_FLOW.md`, `ML_RETRAIN_HANDOVER.md`, this doc — authoritative.
  - ✅ Regime guard on the common path (off by default — `REGIME_GUARD_MAX_ORW=0`).
  - ✅ The `amit-trading` project fixes (skills/docs/scripts).
  - ⚠️ The off-by-default scout gates (chop filter `ENTRY_ALLOWED_REGIMES`, confirmation, conviction gate) — harmless (default off) but optional to keep; they're scouts, marked in CLEANUP-BACKLOG.
  - ⚠️ `docs/ENGINE_DECISION_FLOW.md §9b` lists dormant-but-referenced code to clean up *deliberately* (don't blind-delete — sim tooling depends on it).

---

## 7. The path forward (unchanged, evidence-backed)
1. **ML team runs the entry retrain** on `option-trading-ml-01` (clean-move label, `docs/ML_RETRAIN_HANDOVER.md`). Entry is already good; this is a refinement.
2. **THE real work — direction:** train a direction model on **microstructure (OI/IV/order-book), independent of momentum**, validate for the SIDE + calibration + drop-outlier OOS, then **stack with the 57% fade**. Target 61%+.
3. **If direction still can't beat ~58% → go structural** (sell-side, or longer horizon).
4. **Rebuild the runtime VM** (§4) only when there's a model worth running — then paper-validate → live (1 lot).

**Real money stays OFF until a config is net-positive on a real OOS sample (drop-outlier-safe). No exceptions.**
