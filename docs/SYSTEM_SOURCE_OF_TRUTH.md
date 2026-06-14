# BankNifty System — Source of Truth

As-of date: **2026-06-14**

> If active docs conflict with code, code wins. If active docs conflict with each
> other, this file wins. The previous version of this doc (dated 2026-05-18)
> referenced the dead `ml_pure` engine and old model bundles — it is superseded.

---

## 1. Runtime Engine

**Active engine:** `deterministic_rule_engine` (profile `trader_master_live_v1`).  
The `ml_pure` engine referenced in the May-18 version is **retired** — do not deploy it.

- Entry trigger: `ML_ENTRY` (xgboost entry_only_v3 bundle) or `VOL_GATE_ENTRY` (ATR-based, no ML)
- Direction: weighted direction detector via `REGIME_DIRECTION_SIGNAL=weighted`
- Exits: `EXIT_STRATEGY_MODE=adaptive` (BREAKOUT/TRENDING → lottery, else → scalper)
- Transport: Redis pub/sub + MongoDB persistence
- Execution: `EXECUTION_ADAPTER=dhan` (real) or `paper`

**GCP project:** `amit-trading` (old `algo-trading-496203` and `amittrading-493606` are DEAD — never use)  
**Runtime VM:** `option-trading-runtime-01`, zone `asia-south1-b`  
**ML VM:** `option-trading-ml-01`, zone `asia-south1-b` (stopped when idle; restartable)

---

## 2. Active ML Model

| Asset | Details |
|---|---|
| Bundle | `entry_only_v3` (020pct label, ≥110pt move) |
| AUC | 0.831, ECE 0.009 (well-calibrated) |
| Threshold | `ENTRY_ML_MIN_PROB=0.45` (per model report, fire_rate 0.79%, precision 59%); live .env.compose still has wrong 0.85 — fix manually |
| GCS path | `gs://amit-trading-option-trading-models/published_models/entry_only_v3/` |
| Local path (VM) | bind-mounted from `/opt/option_trading/.data/ml_pipeline/...` |
| xgboost version | **3.2.0** — pin in Dockerfile; mismatch produces garbage probs |

**Retired / do not deploy:**
- `direction_only_v2` — 0.593 AUC in-sample; inverts to 43.9% OOS. Direction detection is a dead end.
- `option_pnl_atm_pe_15_*` bundles (ml_pure era) — wrong engine, retired.

---

## 3. Strategy Findings (the hard verdicts)

See `docs/FINDINGS_2026-06-14.md` for the full evidence base. Summary:

| Component | Verdict |
|---|---|
| Entry magnitude | ✅ SOLVED — AUC 0.83, 4.37× move discrimination |
| Direction (buy-side) | ❌ DEAD — 50.3% 2024, 43.9% 2026 OOS (inverts) |
| S3 Seller | ✅ ONLY ROBUST PATH — 78% win, +₹1,692/trade on 2024 paper |
| Real money | ❌ OFF — seller needs live-cycle paper; buyer has no edge |

---

## 4. Config Contract

**Single source of config:** `/opt/option_trading/.env.compose` on the runtime VM (185-line brain config).  
The `.deploy/runtime-config/` directory (ml_pure C1 stale templates) has been deleted locally; delete it on the GCP VM too if it still exists there.

Critical env vars:
```bash
EXECUTION_ADAPTER=paper           # NEVER change to 'dhan' without live-cycle paper validation
ENTRY_VOL_GATE_ENABLED=0          # 0 = ML_ENTRY active; 1 = VOL_GATE_ENTRY (ATR-based, no ML)
REGIME_DIRECTION_SIGNAL=agreement_lever  # validated ~61% on big moves; combo = slightly higher but stricter
REGIME_W_MOM=0                    # momentum_15m is an ANTI-signal — keep at 0
ENTRY_ML_MIN_PROB=0.45            # recommended per model report (fire_rate 0.79%, precision 59%); live was wrongly set to 0.85
ENTRY_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib  # CORRECT container path
EXIT_STRATEGY_MODE=adaptive       # BREAKOUT/TRENDING → lottery (20% stop); else → scalper
LOTTERY_HARD_STOP_PCT=0.07        # applies to BREAKOUT/TRENDING entries — must set alongside scalper stop
EXIT_SCALPER_HARD_STOP_PCT=0.05   # applies to NON-breakout entries only
```

> **⚠️ KNOWN ISSUES in live .env.compose (fix manually on GCP VM):**
> - `ENTRY_ML_MODEL_PATH` was set to `/app/models/entry_only_v3.joblib` — dir does not exist → vol gate silently OFF
> - `ENTRY_ML_MIN_PROB` was set to `0.85` (set June 12 to dodge degenerate cluster; must revert to `0.45`)
> - Also verify xgboost version: model trained on 3.2.0; container must match or probs are garbage

**Config validation:**
```bash
docker exec strategy_app printenv | python -m ops.config_audit -
```

---

## 5. Two Regime Systems — Quick Reference

See `docs/TWO_REGIME_SYSTEMS.md` for full detail.

1. **Regime enum** — routes which entry strategies are available (AVOID/CHOP → none)
2. **RegimeDirector quality** — gates direction confidence (CHOP → ABSTAIN → no entry side)

These are SEPARATE. `REGIME_ALLOWED=MID,TREND` is a direction quality filter — it
does NOT block entries in Regime-enum BREAKOUT.

---

## 6. Safe Operations

See `docs/CONFIG_SAFE_OPS.md` for the full guide. Key rules:
- Never `docker cp` a fix — commit + rebuild + redeploy
- Only one `.env.compose` — the 185-line brain config
- Pin `xgboost==3.2.0` in Dockerfiles
- Use `run_id` filter on mongo aggregations (deterministic `_id` → stale contamination)
- Run `config_audit.py` before every live session

---

## 7. GCP Commands

```bash
# SSH to runtime VM
gcloud compute ssh option-trading-runtime-01 --zone asia-south1-b --project amit-trading

# Start ML VM (stopped to save cost)
gcloud compute instances start option-trading-ml-01 --zone asia-south1-b --project amit-trading

# On runtime VM — standard lifecycle
cd /opt/option_trading
docker-compose ps
docker exec strategy_app printenv | python -m ops.config_audit -
docker-compose logs --tail=50 strategy_app
```

---

## 8. Canonical Reference Docs

| Doc | Purpose |
|---|---|
| `docs/FINDINGS_2026-06-14.md` | Full evidence base — what works, what doesn't, what to build next |
| `docs/TWO_REGIME_SYSTEMS.md` | Regime enum vs RegimeDirector quality — the naming confusion |
| `docs/CONFIG_SAFE_OPS.md` | How to safely change code and config without losing fixes |
| `docs/RUNTIME_STATE_AND_RECOVERY.md` | VM rebuild guide + config snapshot |
| `docs/strategy_platform/05_CONFIG_REFERENCE.md` | Every env var with default and meaning |
| `docs/runbooks/GCP_DEPLOYMENT.md` | Deploy runbook |
| `docs/runbooks/LIVE_SETUP_GUIDE.md` | Zero-to-live setup |
