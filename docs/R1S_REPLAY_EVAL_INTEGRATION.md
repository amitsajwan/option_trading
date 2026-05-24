# R1S Top-3 — strategy_app + replay + eval integration

**Production research rule:** `R1S_TOP3_S3_COMPOSITE`  
**Paper profile:** `r1s_top3_paper_v1`  
**Rules backtest (audit):** `ml_pipeline_2` rules pipeline  
**Runtime validation:** historical replay → Mongo → dashboard eval UI

---

## Two lanes (use both)

| Lane | Tool | Question it answers |
|------|------|---------------------|
| **Research audit** | `rules_pipeline` matrix on VM | PASS/FAIL quarters/months (t-stat, CI, outlier survival) |
| **Runtime replay** | `strategy_app_historical` + eval UI | Does **our code** fire the same trades with gates, risk, traces? |

Replay will **not** match rules_pipeline trade-for-trade unless snapshots and gates align. Use replay for **integration parity**; use rules_pipeline for **promotion stats**.

---

## Architecture

```text
Parquet (ml_flat + options)
    → strategy_eval_orchestrator (publishes snapshots)
    → Redis HISTORICAL_TOPIC
    → strategy_app_historical (profile r1s_top3_paper_v1)
    → votes / signals / positions / decision_traces → Redis
    → strategy_persistence_app_historical → Mongo (*_historical collections)
    → dashboard /api/strategy/evaluation/*  (eval tab)
    → /historical/replay (replay monitor)
```

---

## 1. Configure `.env.compose`

```dotenv
STRATEGY_ENGINE=deterministic
STRATEGY_PROFILE_ID=r1s_top3_paper_v1
STRATEGY_MIN_CONFIDENCE=0.50
STRATEGY_ROLLOUT_STAGE_HISTORICAL=paper
STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL=1.0

# Optional macro gate (existing risk manager — manual calm-week proxy)
RISK_VIX_HALT_THRESHOLD=18
RISK_VIX_RESUME_THRESHOLD=16

SNAPSHOT_PARQUET_BASE=/app/.data/ml_pipeline/parquet_data
HISTORICAL_TOPIC=market:snapshot:v1:historical
```

Do **not** use `det_prod_v1` or `ml_pure` for R1S validation.

---

## 2. Start stack

See [runbooks/DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md](runbooks/DETERMINISTIC_HISTORICAL_REPLAY_RUNBOOK.md).

```powershell
docker compose --env-file .env.compose --profile historical up -d `
  redis mongo persistence_app_historical strategy_app_historical strategy_persistence_app_historical
docker compose --env-file .env.compose --profile ui up -d dashboard strategy_eval_orchestrator
```

Rebuild `strategy_app_historical` after code changes.

---

## 3. Queue replay from eval UI

1. Open `http://localhost:8008/app?mode=eval`
2. Set **Dataset** = Historical, date range (e.g. `2024-05-01` → `2024-07-31`)
3. **Strategy** filter = `R1S_TOP3_SHORT_CE` (after run completes)
4. Click **Run Replay** — orchestrator publishes parquet snapshots; `strategy_app_historical` trades.

Or API:

```powershell
$body = @{ dataset = "historical"; date_from = "2024-05-01"; date_to = "2024-07-31"; speed = 0 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8008/api/strategy/evaluation/runs" -ContentType "application/json" -Body $body
```

---

## 4. What to inspect

| Surface | URL / API | Use |
|---------|-----------|-----|
| Eval summary | `/app?mode=eval` | Win rate, PF, equity, by_strategy |
| Replay monitor | `/historical/replay` | Progress, topic health |
| Live strategy (replay mode) | `/api/strategy/current/state?mode=replay` | Engine profile, diagnostics |
| Decision traces | Mongo `strategy_decision_traces_historical` | Which gate blocked entries |
| JSONL | `.run/strategy_app_historical/` | Raw signals for debugging |

Filter eval trades by **entry_strategy** = `R1S_TOP3_SHORT_CE`.

---

## 5. Gate layers in replay

| Layer | Source | R1S behavior |
|-------|--------|--------------|
| Rule entry | `R1sTop3ShortCeStrategy` | ORB-down fade, top-3/day score |
| Disqualifiers | Rule JSON | 9:30–14:30, no expiry |
| Regime router | Profile map | R1S only in TRENDING/SIDEWAYS/PRE_EXPIRY/HIGH_VOL; **EXPIRY off** |
| Regime confidence | Engine | **Skipped** for `r1s_top3_paper_v1` (parity with rules backtest) |
| Entry policy | Bypass | No ML quality gate |
| Risk manager | Env | VIX halt, daily DD, consecutive losses |
| Exits | Profile risk + short PnL | 100% stop / 50% target / ~20 bars, `position_side=SHORT` |

---

## 6. Compare replay vs rules_pipeline

After a replay window:

1. Note `run_id` from eval run.
2. Rules pipeline for same dates:

```bash
python -m ml_pipeline_2.scripts.rules_pipeline.run_backtest \
  --rule ml_pipeline_2/configs/rules/r1s_top3/r1s_top3_s3_composite.json \
  --start 2024-05-01 --end 2024-07-31 \
  --output-dir /tmp/r1s_replay_cmp
```

3. Compare: trade count (~40–60/qtr scale), WR band, gate blocks in traces vs raw signal count.

Large gaps usually mean: regime/router, snapshot feature names, or streaming top-3 vs batch top-3.

---

## 7. Live paper (after replay looks sane)

Same profile on live stack:

```dotenv
STRATEGY_ENGINE=deterministic
STRATEGY_PROFILE_ID=r1s_top3_paper_v1
STRATEGY_ROLLOUT_STAGE=paper
```

Operator sheet: [R1S_TOP3_OPERATOR.md](R1S_TOP3_OPERATOR.md)

---

## 8. UI presets (eval tab)

In `/app` tweaks (or URL `?mode=eval`):

- Set strategy filter default via `evalDefaultStrategy=R1S_TOP3_SHORT_CE`
- Use **R1S risk preset** in eval panel: stop 100%, target 50%, trailing off (analysis labels only; engine uses profile risk)

---

## Files

| Component | Path |
|-----------|------|
| Strategy | `strategy_app/engines/strategies/r1s_top3_short_ce.py` |
| Profile | `strategy_app/engines/profiles.py` → `r1s_top3_paper_v1` |
| Rule JSON | `ml_pipeline_2/configs/rules/r1s_top3/r1s_top3_s3_composite.json` |
| Eval service | `market_data_dashboard/strategy_evaluation_service.py` |
| Orchestrator | `strategy_eval_orchestrator/main.py` |
| R1S diagnostics | `market_data_dashboard/diagnostics/r1s_top3.py` |
