# Model selection pipeline

End-to-end candidate evaluation + selection for the option_pnl model family.

## What this solves

The current deployment loop has been:
1. Run an HPO sweep, pick the top trial by `net_pnl_sum`.
2. Push that as a bundle.
3. Discover in runtime that it doesn't deliver.

That loop ranks on a metric (sum of net P&L) which can be carried by 1-5
lucky days. Every deployed candidate so far fails t-stat / outlier-survival
audits when scrutinized properly.

This pipeline replaces the loop with a multi-cell funnel:

- Train a **matrix** of (recipe × threshold × window) cells.
- For each cell, **audit** holdout per-trade returns against two gates:
  - **G1 (statistical edge)**: t > 2 AND bootstrap CI strictly above 0
    AND net-without-top-5-days >= 0 (outlier survival).
  - **G2 (trade-rate sanity)**: 80 ≤ trades ≤ 500 (2-8/day) AND win rate ≥ 55%.
- Rank all cells. PASS-first, then by t-stat.
- A human reads the leaderboard and promotes the winner via `promote_winner.sh`.

Nothing is auto-deployed. The pipeline produces evidence; a human acts on it.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | Orchestrator. Idempotent, resumable, atomic state writes. |
| `audit_run.py` | Per-cell statistical audit (t-stat, CI, outlier-decomp, gates). |
| `daemon.sh` | tmux-detached wrapper. Single-instance. `start`/`status`/`tail`/`stop`. |
| `status.sh` | Read-only one-line status of latest run. No side effects. |
| `promote_winner.sh` | Manual deployment of a chosen bundle. Never automatic. |
| `recipe_matrix.json` | Matrix config (recipes × thresholds × windows). |
| `README.md` | This file. |

## Quick start

```bash
# 1. Start the pipeline (tmux-detached). Idempotent: re-running resumes.
bash ml_pipeline_2/scripts/model_selection/daemon.sh start

# 2. Watch progress
bash ml_pipeline_2/scripts/model_selection/daemon.sh tail
# or one-shot status:
bash ml_pipeline_2/scripts/model_selection/status.sh

# 3. When phase=complete, read the leaderboard
cat /opt/option_trading/ml_pipeline_2/artifacts/model_selection_runs/run_<TS>/leaderboard.md

# 4. If a cell passed, manually promote its bundle (does NOT auto-deploy)
bash ml_pipeline_2/scripts/model_selection/promote_winner.sh \
  /opt/option_trading/.data/ml_pipeline/option_pnl_published_models/<bundle_dir> \
  --threshold 0.55

# 5. Validate the promoted bundle with a clean replay
bash /opt/option_trading/scripts/clean_state_before_replay.sh
curl -X POST http://localhost:8008/api/strategy/evaluation/runs \
  -H "Content-Type: application/json" \
  -d '{"dataset":"historical","date_from":"2024-08-01","date_to":"2024-10-31","speed":1800}'
```

## Output layout

```
<repo>/ml_pipeline_2/artifacts/model_selection_runs/run_<YYYYMMDD>/
├── state.json              # phase, counts, results[]
├── leaderboard.json        # full ranked list
├── leaderboard.md          # human-readable
├── pipeline.log            # append-only run log
├── exit_code               # final exit code (after daemon exits)
└── cells/
    └── <cell_id>/
        ├── train_out/         # raw trainer output
        ├── trades.parquet     # per-trade audit input (hardlink to train_out/...)
        ├── audit.json         # gate results
        └── train.log          # trainer stdout/stderr
```

`cell_id` format: `<RECIPE>_thr<NN>_<YYYYMMDD>_<8hex>` — derived from a hash
of the config so identical configs produce identical ids (deterministic).

## Idempotency + resume

The orchestrator skips any cell whose `audit.json` is already present and
parseable. To force a re-run:

```bash
rm -rf /opt/option_trading/.../cells/<cell_id>
# OR
python -m ml_pipeline_2.scripts.model_selection.pipeline --force ...
```

## Adding more cells later

Edit `recipe_matrix.json` to add a new matrix entry, then re-run the
daemon. New cells will be processed; existing cells are not re-run.

## Audit gate config

Adjust `audit_gates` in `recipe_matrix.json` if you want a tighter or
looser bar. Sensible defaults:

| Gate | Default | Rationale |
|---|---|---|
| `min_trades` | 80 | < 80 over 60 days = <1.5/day, too sparse for stat-sig |
| `max_trades` | 500 | > 500 over 60 days = > 8/day, too noisy |
| `min_win_rate` | 0.55 | sub-55% means the model is barely calibrated |
| `t_min` | 2.0 | standard ~5% significance threshold |
| `ci_must_exclude_zero` | true | belt-and-braces with t-stat |
| `outlier_survival_must_be_nonneg` | true | rejects 1-day-wonder strategies |

## Why these gates and not more (Sharpe, sortino, etc)?

These three (t, CI, outlier survival) catch >90% of the noise-driven
"edge" patterns we've actually observed in this codebase. Sharpe is
sensitive to the volatility-of-daily-returns numerator which is
unstable on small samples; we keep that as a downstream metric, not a
gate. Sortino adds complexity without changing pass/fail decisions on
the leaderboards we've seen.

## What this pipeline does NOT do

- Re-run HPO. HPO is upstream; this pipeline trains with given params.
- Promote any cell to live. `promote_winner.sh` only sets up historical.
- Touch the runtime VM. All work is on the ML VM.
- Test C1-family models. That's a separate retrain (uses `gcp_run_grid.sh`).
