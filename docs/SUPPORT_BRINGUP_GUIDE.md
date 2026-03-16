# Support Bring-Up Guide

Run commands from repo root: `c:\code\option_trading`

This is the operator checklist for live bring-up and first-response rollback.

Supported runtime target for this guide:

- `redis`, `mongo`, `ingestion_app`, `snapshot_app`, `persistence_app`, `strategy_app`, `strategy_persistence_app`
- optional `dashboard`
- `strategy_app --engine deterministic`
- `strategy_app --engine ml_pure`

Legacy historical replay and strategy eval services are out of scope for this guide.

## 1. Preflight

Required files:

- `.env.compose`
- `ingestion_app/credentials.json`
- `config/nse_holidays.json`
- `.run/ml_runtime_guard_live_best_candidate.json` (if ML runtime is enabled)

Redis port contract (canonical):

- Compose Redis is `6379`.
- Host-side CLI runs must use `REDIS_PORT=6379` unless intentionally overriding.
- Keep `ingestion_app/.env` aligned to `6379` to avoid cross-service dotenv conflicts.

Preflight checks:

```powershell
Test-Path .env.compose
Test-Path ingestion_app/credentials.json
Test-Path config/nse_holidays.json
Test-Path .run/ml_runtime_guard_live_best_candidate.json
```

Single-consumer safety check (must be clean before bring-up):

```powershell
docker compose ps
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'strategy_app\.main' } | Select-Object ProcessId,CommandLine
```

Expected:

- exactly one `strategy_app` container for live topic (`strategy_app-1`)
- no extra host-side `python -m strategy_app.main ... --topic market:snapshot:v1` processes

Optional (recommended) token sanity:

```powershell
python -m ingestion_app.kite_auth --verify
```

If token refresh is required:

```powershell
python -m ingestion_app.kite_auth --force
```

## 1.1 Runtime Guard Artifact (Required For ML in capped_live)

`strategy_app.main` enforces these guard keys when ML runtime is enabled:

- `approved_for_runtime=true`
- `offline_strict_positive_passed=true`
- `paper_days_observed>=10`
- `shadow_days_observed>=10`
- optional strict match: `approved_experiment_id`
- optional strict match: `approved_registry`

Reference payload:

```json
{
  "approved_for_runtime": true,
  "offline_strict_positive_passed": true,
  "paper_days_observed": 10,
  "shadow_days_observed": 10,
  "approved_experiment_id": "eq_full_v1__mfe15_gt_5_v1__seg_global_v1__lgbm_shallow_v1__fixed_060",
  "approved_registry": ".run/ml_only_rebaseline_20260305_060047_main/2021-01-01_2022-02-17_5e931d3969/ml/experiments/experiment_registry.csv"
}
```

## 1.2 Best-Candidate Runtime Env (Latest)

For the current best candidate + aggressive-safe risk profile, set these in `.env.compose`:

```env
STRATEGY_ML_ENTRY_REGISTRY=.run/ml_only_rebaseline_20260305_060047_main/2021-01-01_2022-02-17_5e931d3969/ml/experiments/experiment_registry.csv
STRATEGY_ML_ENTRY_EXPERIMENT_ID=eq_full_v1__mfe15_gt_5_v1__seg_global_v1__lgbm_shallow_v1__fixed_060
STRATEGY_ML_ENTRY_THRESHOLD_POLICY=fixed_custom_062
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live_best_candidate.json

RISK_PROFILE=aggressive_safe_v1
RISK_LOT_SIZING_MODE=budget_per_trade
RISK_NOTIONAL_PER_TRADE=50000
RISK_LOT_BUDGET_USES_LOT_SIZE=1
RISK_MAX_DAILY_LOSS_PCT=0.02
RISK_MAX_CONSECUTIVE_LOSSES=3
RISK_MAX_LOTS_PER_TRADE=20
RISK_PER_TRADE_PCT=0.005
RISK_CAPITAL_ALLOCATED=500000
RISK_VIX_HALT_THRESHOLD=15
RISK_VIX_RESUME_THRESHOLD=8
```

Notes:

- `docker-compose.yml` now forwards rollout/guard args into `strategy_app.main`.
- `STRATEGY_ML_ENTRY_THRESHOLD_POLICY` can override threshold policy at runtime (for example `fixed_custom_062`) without retraining.
- ML runtime in `capped_live` requires a valid guard file.
- Keep `INGESTION_COLLECTORS_ENABLED=0` unless you intentionally want dedicated collector processes. With `0`, ingestion serves on-demand data via API.

## 1.3 ML-Pure Run-ID Switch (Strict Safe)

`strategy_app.main` now supports selecting `ml_pure` runtime artifacts by `run_id` + `model_group`.

Example:

```powershell
python -m strategy_app.main `
  --engine ml_pure `
  --ml-pure-run-id 20260308_164057 `
  --ml-pure-model-group banknifty_futures/h15_tp_auto `
  --rollout-stage capped_live `
  --position-size-multiplier 0.25 `
  --ml-runtime-guard-file .run/ml_runtime_guard_live_best_candidate.json
```

Equivalent env variables:

```env
ML_PURE_RUN_ID=20260308_164057
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
```

Strict switch checks (enforced at startup):

- `run_<run_id>.json` exists under:
  - `ml_pipeline_2/artifacts/published_models/<model_group>/reports/training/`
- `publish_decision.decision == PUBLISH` or `publish_status == published`
- `published_paths.model_package` exists
- `published_paths.threshold_report` exists

Do not mix run-id mode with explicit path mode in one launch:

- run-id mode: `--ml-pure-run-id + --ml-pure-model-group`
- explicit mode: `--ml-pure-model-package + --ml-pure-threshold-report`

## 2. Known-Good Minimal Bring-Up

```powershell
docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app strategy_persistence_app
```

Optional supported UI:

```powershell
docker compose --env-file .env.compose --profile ui up -d dashboard
```

Legacy eval UI/orchestrator remain out of scope for this guide and should not be treated as part of the supported fresh-machine Live+Dashboard bring-up.

## 3. First Verification

Container state:

```powershell
docker compose ps
```

Health probes:

```powershell
curl http://127.0.0.1:8004/health
curl http://127.0.0.1:8008/api/health
```

Log probes:

```powershell
docker compose logs --tail 100 ingestion_app
docker compose logs --tail 100 snapshot_app
docker compose logs --tail 100 strategy_app
docker compose logs --tail 100 strategy_persistence_app
```

Kite/auth + collector mode probe:

```powershell
docker compose logs --tail 120 ingestion_app
```

Expected in logs:

- no `Incorrect api_key or access_token`
- either collectors started (`INGESTION_COLLECTORS_ENABLED=1`) or explicit on-demand mode message (`INGESTION_COLLECTORS_ENABLED=0`)
- strategy_app startup line includes one runtime mode only (no accidental mixed paper/capped-live consumers)

Data flow probes:

```powershell
Get-Content .run/snapshot_app/events.jsonl -Tail 5
Get-Content .run/strategy_app/votes.jsonl -Tail 5
Get-Content .run/strategy_app/signals.jsonl -Tail 5
```

Expected:

- snapshots continue during market session
- strategy votes/signals update when snapshots update
- no repeated startup exceptions in service logs
- no duplicate/triplicate votes for the same `snapshot_id` from multiple live consumers

## 3.1 Duplicate Consumer Guard (Live)

`strategy_app` enforces a Redis lock per snapshot topic:

- lock key format: `strategy_app:consumer_lock:<topic>`
- second consumer on the same topic exits with `duplicate strategy consumer detected`
- lock auto-refreshes with TTL and is released on clean shutdown

If `/live/strategy` looks duplicated or inconsistent:

```powershell
docker compose --env-file .env.compose logs strategy_app --tail 120
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'strategy_app\.main' } | Select-Object ProcessId,CommandLine
```

Then stop extra host processes and recreate `strategy_app`:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'strategy_app\.main --engine deterministic --topic market:snapshot:v1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
docker compose --env-file .env.compose up -d --force-recreate strategy_app
```

## 4. Runtime ML Safety Check

By default, runtime ML should remain off.  
If intentionally enabled, all of the following must hold:

- `ML_ENTRY_REGISTRY` and `ML_ENTRY_EXPERIMENT_ID` both present
- optional threshold override present only when intended: `ML_ENTRY_THRESHOLD_POLICY`
- `--rollout-stage capped_live`
- `--position-size-multiplier <= 0.25`
- approval guard file provided and valid

If `--engine ml_pure` with run-id switch is enabled:

- both `ML_PURE_RUN_ID` and `ML_PURE_MODEL_GROUP` (or CLI equivalents) are provided
- resolved run report decision is `PROMOTE`
- resolved model package and threshold report files exist

Quick check:

```powershell
docker compose exec strategy_app sh -lc "env | sort | grep 'ML_ENTRY'"
docker compose exec strategy_app sh -lc "env | sort | grep 'ML_PURE'"
docker compose exec strategy_app sh -lc "env | sort | grep 'RISK_'"
docker compose logs strategy_app --tail 60
```

Expected startup line includes:

- `rollout_stage=capped_live`
- `size_multiplier=0.25` (or lower)
- `ml_entry_experiment_id=<your id>`
- `ml_entry_threshold_policy=<policy>` or `registry_default`
- if `ml_pure` run-id mode is enabled:
  - `ml_pure_run_id=<run id>`
  - `ml_pure_model_group=<model group>`

## 5. Rollback / Fallback (Deterministic-Only)

If live behavior is abnormal, revert to deterministic-only by clearing ML env and recreating `strategy_app`.

```powershell
Remove-Item Env:ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_ENTRY_THRESHOLD_POLICY -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_REGISTRY -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_EXPERIMENT_ID -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_ENTRY_THRESHOLD_POLICY -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_RUN_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_GROUP -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_PACKAGE -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_THRESHOLD_REPORT -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ML_RUNTIME_GUARD_FILE -ErrorAction SilentlyContinue
Remove-Item Env:RISK_PROFILE -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_ROLLOUT_STAGE -ErrorAction SilentlyContinue
Remove-Item Env:STRATEGY_POSITION_SIZE_MULTIPLIER -ErrorAction SilentlyContinue
docker compose --env-file .env.compose up -d --force-recreate strategy_app
```

## 6. Stop

```powershell
docker compose --env-file .env.compose down --remove-orphans
```

## 7. Escalation Data to Capture

Before escalating, collect:

- `docker compose ps`
- last 200 lines from ingestion/snapshot/strategy/strategy_persistence logs
- last 20 lines from `.run/snapshot_app/events.jsonl`
- last 20 lines from `.run/strategy_app/{votes,signals,positions}.jsonl`

## 8. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [PROCESS_TOPOLOGY.md](PROCESS_TOPOLOGY.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DOCS_CODE_MAP.md](DOCS_CODE_MAP.md)
