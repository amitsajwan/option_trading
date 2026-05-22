# ML playground overnight (entry + direction)

One-command overnight run on **`option-trading-runtime-01`**: full Optuna HPO for entry and direction, then per–feature-set grids for comparison.

## What runs

| Phase | Manifest | ~ETA | Output |
|-------|----------|------|--------|
| Entry HPO | `staged_dual_recipe.entry_s1_only_hpo_v2.json` | 2–3 h | `artifacts/research/entry_s1_only_hpo_v2_*` |
| Direction HPO | `staged_dual_recipe.direction_s2_only_hpo_v2.json` | 3–5 h | `artifacts/research/direction_s2_only_hpo_v2_*` |
| Entry grid | `staged_grid.entry_playground_v1.json` | 2–3 h | `artifacts/research/staged_grid_entry_playground_v1_*` |
| Direction grid | `staged_grid.direction_playground_v1.json` | 2–3 h | `artifacts/research/staged_grid_direction_playground_v1_*` |

**Models searched (HPO):** xgb / lgbm / logreg families with Optuna (32 trials per model on full HPO; 24 per lane on grids).

**Feature sets (both tracks):**

- `fo_velocity_v1`
- `fo_midday_direction_regime_v1`
- `fo_midday_time_aware_plus_oi_iv`
- `fo_midday_asymmetry`
- `fo_oi_pcr_momentum`

**Session filter:** MIDDAY + LATE_SESSION (velocity/regime features populated).

## Deploy and launch

```bash
# Local: commit + push, then on VM:
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=algo-trading-496203 --command "
  sudo bash -c 'cd /opt/option_trading && git fetch origin main && git checkout main && git pull --ff-only origin main && git log -1 --oneline'
"

# Preflight (imports, parquet, RAM, manifests)
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=algo-trading-496203 --command "
  sudo bash /opt/option_trading/ops/gcp/run_ml_playground_overnight_vm.sh preflight
"

# Full night (HPO + grids) — auto tmux + compose down + runs as amits
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=algo-trading-496203 --command "
  sudo bash /opt/option_trading/ops/gcp/run_ml_playground_overnight_vm.sh start
"

# Morning status
gcloud compute ssh option-trading-runtime-01 --zone=asia-south1-b --project=algo-trading-496203 --command "
  sudo bash /opt/option_trading/ops/gcp/run_ml_playground_overnight_vm.sh status
  sudo bash /opt/option_trading/ops/gcp/summarize_ml_playground_overnight.sh
"
```

## Modes

```bash
PLAYGROUND_MODE=hpo   # entry + direction HPO only (~6–8 h)
PLAYGROUND_MODE=grid  # feature grids only (~5–7 h)
PLAYGROUND_MODE=all   # default: HPO then grids (~12–14 h)
```

## Logs

- Master: `/tmp/ml_playground_overnight/master.log`
- Per phase: `01_entry_hpo.log`, `02_direction_hpo.log`, `03_entry_grid.log`, `04_direction_grid.log`

## After results

**Entry bundle** (if publishable or best-effort export):

```bash
python -m ml_pipeline_2.scripts.export_entry_bundle_from_research \
  --run-dir ml_pipeline_2/artifacts/research/entry_s1_only_hpo_v2_<timestamp>
```

**Direction bundle:**

```bash
python -m ml_pipeline_2.scripts.export_direction_bundle_from_research \
  --run-dir ml_pipeline_2/artifacts/research/direction_s2_only_hpo_v2_<timestamp>
```

Then replay Aug–Oct with new `ENTRY_ML_*` threshold grid; direction stays `DET_DIRECTION` until S2 model is validated.

See also [ENTRY_AND_DIRECTION.md](ENTRY_AND_DIRECTION.md).
