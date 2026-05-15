# Backtest analysis scripts

Tools for analyzing historical replay output **without using mongo** (mongo persistence is unreliable under replay load; JSONL is the canonical source of truth).

## `analyze_jsonl.py`

Reads `positions.jsonl` written by `strategy_app_historical` and reports per-window statistics. Splits trades by C1 model's train/valid/holdout windows so in-sample contamination is visible at a glance.

Run on the runtime VM (where the JSONL is mounted at `/opt/option_trading/.run/strategy_app_historical/`):

```bash
sudo python3 /home/amits/analyze_jsonl.py                  # latest run, all windows
sudo python3 /home/amits/analyze_jsonl.py --run-id 5eb9e3d9 # specific run by prefix
sudo python3 /home/amits/analyze_jsonl.py --list           # list all run_ids
sudo python3 /home/amits/analyze_jsonl.py --window holdout # holdout-only summary
```

The script flags `⚠ SAMPLE TOO SMALL` when holdout has fewer than 30 trades, since OOS conclusions below that threshold are not statistically meaningful.

**Windows hard-coded to C1 (`staged_deep_hpo_c1_base_20260429_040848`).** When the live model changes, update the `C1_TRAIN_END` / `C1_VALID_END` / `C1_HOLDOUT_END` constants at the top of the script.

## `sim_exit_sweep.js`

Counterfactual exit-timing sweep on a fixed entry set. Used to test "does a different `max_hold_bars` value help on the holdout window?" without re-running the strategy_app live.

Reads C1's 107 baseline entries from `strategy_positions_historical`, then for each `maxHold ∈ {9, 15, 20, 30}` walks forward through `phase1_market_snapshots_historical` applying:

- `STOP_PCT = 0.002` (20 bps adverse underlying move → STOP_LOSS)
- `TARGET_PCT = 0.005` (50 bps favorable underlying move → TARGET_HIT)
- Otherwise TIME_STOP at the maxHold bar

Reports per-window summaries and a side-by-side holdout-only comparison. Useful to see whether exit timing is the lever (if any variant shows a positive holdout PF) or not (if all are net-negative, the model lacks OOS edge regardless).

Run inside the mongo container:

```bash
sudo docker cp scripts/sim_exit_sweep.js option_trading-mongo-1:/tmp/sweep.js
sudo docker exec option_trading-mongo-1 mongosh trading_ai --quiet --file /tmp/sweep.js
```

The script takes ~3 minutes to complete (4 variants × 107 entries × ~30 snapshot lookups each ≈ 13,000 mongo queries).

## `run_f1_handoff.sh`

Polls the ML VM for walkforward F1 training completion, then dumps the F1 summary and prints the manual next steps (model publish + 2024 replay).

```bash
bash scripts/run_f1_handoff.sh
```

Polls every 10 minutes. Idempotent — safe to leave running overnight.

## `ingest_kite_historical.py` (SKELETON)

Skeleton for 2025 BankNifty data ingestion from Kite Connect Historical API. **Not runnable as-is** — needs Kite Historical subscription (~₹2000/month) + filled-in TODOs. Documents the data shape, rate limits, and ingestion approach. Use as the starting point once the subscription is paid.

```bash
KITE_API_KEY=... KITE_ACCESS_TOKEN=... python3 scripts/ingest_kite_historical.py --from 2025-01-01 --to 2025-12-31 --dry-run
```

## Why these live in `scripts/` and not `tools/`

They are operator-side analytical tools, not part of any deployed service. They read JSONL files and run mongo queries from outside the strategy_app codebase. They do not depend on the strategy_app Python package and should not be imported from it.
