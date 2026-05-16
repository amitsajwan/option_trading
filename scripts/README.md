# Backtest analysis scripts

Tools for analyzing historical replay output. **JSONL is canonical** for trade events — these scripts read it directly or query mongo as a derived cache, depending on what's most current.

## `analyze_jsonl.py`

Reads `positions.jsonl` written by `strategy_app_historical` and reports per-window statistics. Splits trades by the live model's train/valid/holdout windows so in-sample contamination is visible at a glance.

Run on the runtime VM (where the JSONL is mounted at `/opt/option_trading/.run/strategy_app_historical/`):

```bash
sudo python3 /home/amits/analyze_jsonl.py                  # latest run, all windows
sudo python3 /home/amits/analyze_jsonl.py --run-id 5eb9e3d9 # specific run by prefix
sudo python3 /home/amits/analyze_jsonl.py --list           # list all run_ids
sudo python3 /home/amits/analyze_jsonl.py --window holdout # holdout-only summary
```

Flags `⚠ SAMPLE TOO SMALL` when holdout has fewer than 30 trades, since OOS conclusions below that threshold are not statistically meaningful.

**Windows hard-coded to C1 (`staged_deep_hpo_c1_base_20260429_040848`).** When the live model changes, update the `C1_TRAIN_END` / `C1_VALID_END` / `C1_HOLDOUT_END` constants at the top of the script.

## `sim_exit_sweep.js`

Counterfactual exit-timing sweep on a fixed entry set. Used to test "does a different `max_hold_bars` value help on the holdout window?" without re-running the strategy_app live.

Reads C1's 107 baseline entries from `strategy_positions_historical`, then for each `maxHold ∈ {9, 15, 20, 30}` walks forward through `phase1_market_snapshots_historical` applying:

- `STOP_PCT = 0.002` (20 bps adverse underlying move → STOP_LOSS)
- `TARGET_PCT = 0.005` (50 bps favorable underlying move → TARGET_HIT)
- Otherwise TIME_STOP at the maxHold bar

Reports per-window summaries and a side-by-side holdout-only comparison. Used 2026-05-15 to confirm exit timing is NOT the lever — all 4 variants are net-negative on holdout.

Run inside the mongo container:

```bash
sudo docker cp scripts/sim_exit_sweep.js option_trading-mongo-1:/tmp/sweep.js
sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --file /tmp/sweep.js
```

Takes ~3 minutes (4 variants × 107 entries × ~30 snapshot lookups each ≈ 13,000 mongo queries).

## Live-state observation (no script needed)

The dashboard now exposes `/api/strategy/current/state?mode=live|replay` that reads JSONL directly (no mongo dependency). Use this for "what's running right now" — equivalent to `tail -n 50 positions.jsonl | jq .` but as a structured response with health-marker state and roll-up stats. Per [ARCHITECTURE.md §9](../docs/ARCHITECTURE.md), this is the JSONL-first split for current-session queries; cross-day aggregates continue to use the existing mongo-backed routes.

## Training-orchestration scripts

Deleted (2026-05-16) — `run_f1_handoff.sh` and `launch_pathb1_when_f1_done.sh` were specific to the F1+B1 chained experiments from 2026-05-15. Both runs are now in the completed-training record (see [ml_pipeline_2/docs/training/MODEL_STATE_20260515.md](../ml_pipeline_2/docs/training/MODEL_STATE_20260515.md)). Future chained experiments should write fresh launchers parameterized for their specific manifest_hashes; copy-pasting the deleted ones with stale hashes would be misleading.

## Why these live in `scripts/` and not `tools/`

They are operator-side analytical tools, not part of any deployed service. They read JSONL files and run mongo queries from outside the strategy_app codebase. They do not depend on the strategy_app Python package and should not be imported from it.
