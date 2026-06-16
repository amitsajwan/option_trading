# Playbook v1 — operator runbook

## Production candidate (2026-05-21)

| Item | Value |
|------|--------|
| **Research default** | `PBV1_TOP3_THESIS` (`pbv1_top3_thesis.json`) — no tight premium cap |
| **Paper candidate (hard + smart)** | `PBV1_TOP3_TRADER_V1` (`pbv1_top3_trader_v1.json`) — 15% premium stop, calm, thesis, trail |
| **Runtime profile** | `playbook_v1_paper_v1` |
| **Strategy name in logs** | `PBV1_TOP3_THESIS` (strategy id; rule id is in vote reason text) |
| **Experiment** | `PBV1_TOP3_PRODUCTION_V1` — 50% premium stop |

```bash
# Default thesis (research parity)
PLAYBOOK_V1_RULE_PATH=/app/ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis.json

# Paper candidate — hard 15% premium stop + calm + thesis + trail
PLAYBOOK_V1_RULE_PATH=/app/ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_trader_v1.json

# Production experiment (calm + 50% premium cap)
# PLAYBOOK_V1_RULE_PATH=/app/ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_production_v1.json
```

## Deploy checklist (runtime VM)

Every brain or rule change requires **all** of:

1. `git pull` / `reset --hard origin/main`
2. `bash ops/gcp/patch_playbook_v1_env.sh .env.compose [optional_rule_path]`
3. `docker compose build strategy_app_historical` (image copies `strategy_app` + `ml_pipeline_2`)
4. `docker compose --profile historical up -d --force-recreate strategy_app_historical`
5. Logs must show:
   - `profile=playbook_v1_paper_v1`
   - `TRENDING -> ['PBV1_TOP3_THESIS']`
   - `strategy consumer subscribed topic=market:snapshot:v1:historical`

## Before queuing a replay

```bash
cd /opt/option_trading
sudo python3 ops/gcp/preflight_historical_replay.py
# expect PREFLIGHT_OK
```

Then queue (example Aug–Oct):

```bash
sudo python3 ops/gcp/queue_overnight_replays.py
# or production check:
# .venv/bin/python3 -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
#   --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_playbook_v1_production_check.json \
#   --output-root ml_pipeline_2/artifacts/rules_runs/playbook_v1_production_check_$(date +%Y%m%d)
```

## Eval UI

- Open: `http://<runtime-ip>:8008/app/?mode=eval`
- Use **Replay runs** dropdown (not only date filters).
- Deep link: `?mode=eval&run_id=<uuid>&date_from=...&date_to=...`
- **Ignore runs with 0 trades** if replay ran while `strategy_app_historical` was down.

### Known good replays (thesis, Aug–Oct 2024)

| Label | run_id |
|-------|--------|
| Thesis baseline | `8f3efb0a-7bae-4229-89ab-85c5f1aa546a` |
| Trail+stop50 (experiment) | `0ee88130-ce63-4153-8138-16d9382f2809` |
| Empty (consumer down) | `8d178356-89a4-4690-8487-d59009d34d2a` — discard |

## Rules ↔ runtime parity (one day)

On VM (needs parquet data on ML path or VM data mount):

```bash
sudo python3 ops/gcp/compare_rules_runtime_day.py \
  --date 2024-09-24 \
  --rule ml_pipeline_2/configs/rules/playbook_v1/pbv1_top3_thesis.json \
  --run-id 8f3efb0a-7bae-4229-89ab-85c5f1aa546a
```

Expect `PARITY_OK` or a printed mismatch on exit reason / option PnL%.

## Risk interpretation

- Eval **Capital PnL%** uses full premium notional vs $1k initial — tail days look worse than **Option PnL%**.
- `stop_pct: 100` in thesis = **no premium disaster cap** (only 0.3% underlying + thesis + time).
- Product goal is **thesis exits**, not a 50% take-profit label.
