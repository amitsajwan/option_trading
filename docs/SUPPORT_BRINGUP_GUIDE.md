# Support Bring-Up Guide

Run commands from repo root.

Supported live runtime target:

- `strategy_app --engine ml_pure`
- `snapshot_app`
- `persistence_app`
- `strategy_persistence_app`

Deterministic mode is replay-only and is not the supported live lane.

## 1. Preflight

Required files:

- `.env.compose`
- `ingestion_app/credentials.json`
- `config/nse_holidays.json`
- runtime guard JSON when `ml_pure` is started in `capped_live`

## 2. Required Live Env

Set in `.env.compose`:

```env
STRATEGY_ENGINE=ml_pure
ML_PURE_RUN_ID=<run_id>
ML_PURE_MODEL_GROUP=banknifty_futures/h15_tp_auto
STRATEGY_ROLLOUT_STAGE=capped_live
STRATEGY_POSITION_SIZE_MULTIPLIER=0.25
STRATEGY_ML_RUNTIME_GUARD_FILE=.run/ml_runtime_guard_live.json
```

You may use explicit artifact paths instead of run-id mode, but do not mix both modes in one launch.

## 3. Bring-Up

```powershell
docker compose --env-file .env.compose up -d --build redis mongo ingestion_app snapshot_app persistence_app strategy_app strategy_persistence_app
```

## 4. Verification

Check:

- `docker compose ps`
- `docker compose logs --tail 100 strategy_app`
- `.run/strategy_app/signals.jsonl`

Expected:

- startup line shows `engine=ml_pure`
- resolved run-id or artifact paths are present
- no duplicate strategy consumer error

## 5. Rollback

To fall back to replay-safe deterministic behavior for investigation:

```powershell
$env:STRATEGY_ENGINE='deterministic'
docker compose --env-file .env.compose up -d --force-recreate strategy_app
```

Use this only for controlled replay or diagnosis, not as the supported live target.

## 6. Related Docs

- [SYSTEM_SOURCE_OF_TRUTH.md](SYSTEM_SOURCE_OF_TRUTH.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [../strategy_app/docs/STRATEGY_ML_FLOW.md](../strategy_app/docs/STRATEGY_ML_FLOW.md)
- [../strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md](../strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md)
