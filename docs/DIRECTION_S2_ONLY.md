# Decoupled direction ML (Stage 2 only)

## Strategy

1. **Entry first** — playbook / Stage-1 research (`staged_dual_recipe.stage1_hpo.json`, PBV1 rules).
2. **Direction later** — this path trains **CE vs PE** only, for `DIRECTION_ML_MODEL_PATH` overlay when rules disagree.

The old `direction_only_hpo_v1` manifest still ran **all three stages**; publish gates measured the full stack (often 0 trades). Use **`direction_s2_only_hpo_v1`** instead.

## Manifest

`ml_pipeline_2/configs/research/staged_dual_recipe.direction_s2_only_hpo_v1.json`

| Flag | Effect |
|------|--------|
| `bypass_stage1` | No entry model training; `entry_prob=1` for direction policy tuning |
| `bypass_stage3` | No recipe model training |
| `direction_only_publish` | Holdout = direction economic PnL (oracle CE/PE), not 3-stage combined |
| `direction_market_up_all_v1` | Labels on all rows (no `entry_label` gate) |

## Run on unified VM

```bash
# Stop compose to free RAM
cd /opt/option_trading
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml down

sudo bash ops/gcp/run_direction_s2_only_hpo_vm.sh validate
sudo bash ops/gcp/run_direction_s2_only_hpo_vm.sh
sudo bash ops/gcp/run_direction_s2_only_hpo_vm.sh status
```

Expect ~1–2 h (oracle + S2 HPO only; no S3).

## Export for strategy_app

```bash
RUN=ml_pipeline_2/artifacts/research/direction_s2_only_hpo_v1_<timestamp>
.venv/bin/python -m ml_pipeline_2.scripts.export_direction_bundle_from_research \
  --run-dir "$RUN" \
  --output-dir ml_pipeline_2/artifacts/direction_only/published

export DIRECTION_ML_MODEL_PATH=/opt/option_trading/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib
# optional: DIRECTION_ML_WEIGHT=0.4  DIRECTION_ML_FILTER_MIN_PROB=0.52
```

Restart `strategy_app` after setting env (see `strategy_app/ml/direction_ml_policy.py`).

## Publish gates

- **Stage 2 holdout AUC** ≥ 0.52 (classification quality)
- **direction_only** economic gates: PF ≥ 1, trades ≥ 30, side balance, etc.

`publishable: true` means the direction model passed **decoupled** checks — not that the full staged bundle is live-ready.
