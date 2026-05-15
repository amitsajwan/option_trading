# Backtest analysis + training-orchestration scripts

Tools for analyzing historical replay output **without using mongo** (mongo persistence is unreliable under replay load; JSONL is the canonical source of truth), plus light-weight orchestration for the ML VM training runs.

## `analyze_jsonl.py`

Reads `positions.jsonl` written by `strategy_app_historical` and reports per-window statistics. Splits trades by C1 model's train/valid/holdout windows so in-sample contamination is visible at a glance.

Run on the runtime VM (where the JSONL is mounted at `/opt/option_trading/.run/strategy_app_historical/`):

```bash
sudo python3 /home/amits/analyze_jsonl.py                  # latest run, all windows
sudo python3 /home/amits/analyze_jsonl.py --run-id 5eb9e3d9 # specific run by prefix
sudo python3 /home/amits/analyze_jsonl.py --list           # list all run_ids
sudo python3 /home/amits/analyze_jsonl.py --window holdout # holdout-only summary
```

Flags `⚠ SAMPLE TOO SMALL` when holdout has fewer than 30 trades, since OOS conclusions below that threshold are not statistically meaningful.

**Windows hard-coded to C1 (`staged_deep_hpo_c1_base_20260429_040848`).** When the live model changes (e.g., to F1 or B1), update the `C1_TRAIN_END` / `C1_VALID_END` / `C1_HOLDOUT_END` constants at the top of the script.

## `sim_exit_sweep.js`

Counterfactual exit-timing sweep on a fixed entry set. Used to test "does a different `max_hold_bars` value help on the holdout window?" without re-running the strategy_app live.

Reads C1's 107 baseline entries from `strategy_positions_historical`, then for each `maxHold ∈ {9, 15, 20, 30}` walks forward through `phase1_market_snapshots_historical` applying:

- `STOP_PCT = 0.002` (20 bps adverse underlying move → STOP_LOSS)
- `TARGET_PCT = 0.005` (50 bps favorable underlying move → TARGET_HIT)
- Otherwise TIME_STOP at the maxHold bar

Reports per-window summaries and a side-by-side holdout-only comparison. Used today (2026-05-15) to confirm exit timing is NOT the lever — all 4 variants are net-negative on holdout.

Run inside the mongo container:

```bash
sudo docker cp scripts/sim_exit_sweep.js option_trading-mongo-1:/tmp/sweep.js
sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --file /tmp/sweep.js
```

Takes ~3 minutes (4 variants × 107 entries × ~30 snapshot lookups each ≈ 13,000 mongo queries).

## `run_f1_handoff.sh`

Polls the ML VM for walkforward F1 training completion (matched by manifest_hash), dumps the F1 summary, and prints the manual next-step checklist (model publish + 2024 replay).

```bash
bash scripts/run_f1_handoff.sh
```

Polls every 10 minutes. Idempotent — safe to leave running overnight. Does NOT launch B1 — use `launch_pathb1_when_f1_done.sh` for that.

## `launch_pathb1_when_f1_done.sh`

Polls F1. When F1 reaches `status='completed'`, automatically launches Path B1 (option-aware label retrain with `cost_per_trade=0.02`) in a new tmux session on the ML VM. If F1 `failed` or `error`, exits without launching B1 — leaves the decision to the operator.

```bash
bash scripts/launch_pathb1_when_f1_done.sh
```

Use case: leave overnight; wake up to either F1 result + B1 progressing, or F1 result + a decision point waiting.

## Experiment configs (where Path A / F1 / B1 live)

The training-side configs are in [`ml_pipeline_2/configs/research/`](../ml_pipeline_2/configs/research/):

- `staged_dual_recipe.deep_hpo_c1.json` — baseline C1 (live model, original windows, 6 bps cost-in-label)
- `staged_dual_recipe.walkforward_f1_no2024.json` — F1: same recipe, shifted 12 months back so 2024 is OOS
- `staged_dual_recipe.deep_hpo_b1_optcost_200bps.json` — B1: same recipe + windows as C1, but `cost_per_trade=0.02` in label

C1 vs F1 = same recipe under window shift (tests temporal generalization).
C1 vs B1 = same recipe and windows under cost-in-label shift (tests if model survives realistic option friction).
Run together over the next 12-24 hours = factorial design across both dimensions.

## Why these live in `scripts/` and not `tools/`

They are operator-side analytical and orchestration tools, not part of any deployed service. They read JSONL files and run mongo queries from outside the strategy_app codebase. They do not depend on the strategy_app Python package and should not be imported from it.
