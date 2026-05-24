# OOS validation — ML_ENTRY primary voter

Validates the **frozen** breakthrough config on a date range **disjoint** from Aug–Oct 2024 before tuning caps, TIME_STOP, or council exits.

## Pass bar (per OOS window)

| Gate | Threshold |
|------|-----------|
| Closed trades | **≥ 40** |
| Portfolio profit factor (cap-weighted) | **≥ 1.30** |
| CE leg PF | **≥ 1.00** |
| PE leg PF | **≥ 1.00** |
| Stop config sanity | `stop_loss_pct` ≈ 20% on closes (not 40% bug) |

**Fail any gate → do not relax `session_trade_cap` / `risk_pause` or tune TIME_STOP.**

## OOS windows (parquet ends 2024-10-31)

| Label | Dates | Role |
|-------|--------|------|
| `oos_primary` | 2024-05-01 → 2024-07-31 | **Primary OOS** — 3 months immediately before in-sample |
| `oos_secondary` | 2023-05-01 → 2023-07-31 | **Secondary OOS** — same season, prior year |
| `in_sample_sanity` | 2024-08-01 → 2024-10-31 | Optional regression check (~61 trades, PF ~1.98) |

Do **not** queue Nov 2024–Jan 2025 until snapshot parquet extends past 2024-10-31.

## VM procedure (unified host)

```bash
cd /opt/option_trading
git fetch origin main && git checkout main && git pull --ff-only origin main

# Rebuild historical consumer (strategy_app changes)
sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  build strategy_app_historical
sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --force-recreate --pull never strategy_app_historical

# One-shot: patch env, clean state, preflight, queue, wait, analyze
sudo bash ops/gcp/run_oos_validation_replay.sh oos_primary
# Optional second window:
sudo bash ops/gcp/run_oos_validation_replay.sh oos_secondary
```

### Manual steps (if not using orchestrator)

```bash
export ENTRY_ML_MIN_PROB=0.65
sudo bash ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh /opt/option_trading/.env.compose
# rebuild historical (see above)

sudo bash ops/gcp/clean_state_before_replay.sh
sudo /opt/option_trading/.venv/bin/python3 ops/gcp/preflight_historical_replay.py

/opt/option_trading/.venv/bin/python3 ops/gcp/queue_replay.py 2024-05-01 2024-07-31

# After run completes:
RUN_ID=$(curl -fsS 'http://127.0.0.1:8008/api/strategy/evaluation/runs/latest?dataset=historical' | jq -r '.run_id // .run.run_id')
sudo docker exec option_trading-dashboard-1 python /opt/option_trading/ops/gcp/analyze_oos_validation_run.py "$RUN_ID" oos_primary
```

## Analysis output

`analyze_oos_validation_run.py` prints:

- Trade count, WR, cap PF, CE/PE leg PF
- Exit-reason mix, monthly breakdown
- Blocker histogram from `strategy_decision_traces_historical` (`primary_blocker_gate`)
- ML_ENTRY vote funnel (votes vs closes)
- **PASS / FAIL** table vs gates above

Exit code **0** = pass, **1** = fail (for scripting).

## After OOS

| Result | Action |
|--------|--------|
| **Pass both OOS windows** | B) Pilot higher `RISK_MAX_SESSION_TRADES` (8→10); then A) TIME_STOP / MFE giveback |
| **Pass primary only** | Run secondary; hold tuning |
| **Fail trade count** | Check blockers (`risk_pause`, `session_trade_cap`); do not lower `ENTRY_ML_MIN_PROB` without hypothesis |
| **Fail PF with balanced legs** | Review TIME_STOP / stop-out mix; still no cap tuning until sample ≥ 40 |

## References

- [BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md](../BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md)
- `ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh`
- `.cursor/rules/gcp-deploy-workflow.mdc`
