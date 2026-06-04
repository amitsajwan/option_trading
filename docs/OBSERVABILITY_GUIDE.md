# Observability Guide

> **As-of:** 2026-05-19 · **Operator-focused**
>
> Where do I look to answer X? Every operational question maps to a
> specific file or grep pattern. If something doesn't appear here, it
> isn't currently observable — flag it as an observability gap to fix.

For the gate chain semantics, see [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md).
For what the model emits, see [`MODEL_OUTPUT_CONTRACT.md`](MODEL_OUTPUT_CONTRACT.md).
For backups, restores, and the cleanup protocol, see [`runbooks/CLEANUP_ROLLBACK_RUNBOOK.md`](runbooks/CLEANUP_ROLLBACK_RUNBOOK.md).

---

## TL;DR — the 5 files that matter

All under `/opt/option_trading/.run/strategy_app{,_historical}/`:

| File | What it is | Lifecycle |
|---|---|---|
| `runtime_state.json` | Current state snapshot (engine, last event, hold counts) | Rewritten every snapshot |
| `signals.jsonl` | Every signal: ENTRY, EXIT, HOLD (with reason) | Append-only, one line per signal |
| `positions.jsonl` | Position events: OPEN, MANAGE, CLOSE | Append-only, one line per event |
| `decisions.jsonl` | **One lean line per tick**: input → what happened → output / blocking gate | Append-only, always-on |
| `decision_traces.jsonl` | Full decision context per snapshot (env-gated, deep) | Append-only |
| `metrics.jsonl` | Engine metrics events (session start, entry, exit) | Append-only |

JSONL is canonical. Mongo is a derived read cache (the dashboard reads
Mongo; the audit harness reads JSONL).

---

## Question → answer cheat sheet

### "Did a trade fire at minute T?"

```bash
grep '"snapshot_id":"YYYYMMDD_HHMM"' .run/strategy_app_historical/signals.jsonl \
  | grep '"signal_type":"ENTRY"'
```

If you get a hit, the trade fired. The line includes the `decision_metrics`
(entry_prob, recipe_id, recipe_margin) and the resulting recipe params.

### "What happened at minute T?" (one-grep answer)

`decisions.jsonl` has exactly one line per evaluate() call — the canonical
"what happened this tick" record. Look here first; the env-gated
`decision_traces.jsonl` is the deep layer beneath it.

```bash
grep '"snapshot_id":"YYYYMMDD_HHMM"' .run/strategy_app_historical/decisions.jsonl \
  | jq '{action, blocking_gate, input, votes, output}'
```

Each line carries:

| Field | Meaning |
|---|---|
| `action` | `entry_taken` / `exit_taken` / `manage_only` / `blocked` / `hold` |
| `blocking_gate` | The decisive gate that stopped an entry (null when a trade fired). Includes numbers, e.g. `stop_loss_cooldown:2<5`, `direction_evidence_mismatch:PE` |
| `input` | `session_phase`, `fut_close`, `atm_strike`, `or_width`, `regime`, `regime_conf` |
| `engine_state` | `has_position`, `is_halted`, `is_paused`, `warmup_blocked`, session P&L / trade count / consecutive losses, `bars_evaluated` |
| `votes` | Per-strategy: `strategy`, `direction`, `confidence`, `grade`, `tier` |
| `output` | When a signal fired: `signal_type`, `direction`, `strike`, `exit_reason`, `grade`, `tier`, `execution_path` |
| `position` | When a position is open: id, direction, strike, bars_held, pnl_pct |

This is the single source of truth for the per-tick decision. The
human-readable `logger.info("entry blocked: …")` narration that used to be
scattered through the engine has been removed — `blocking_gate` replaces it.

### "Why didn't a trade fire at minute T?"

Fastest: `jq '.blocking_gate'` on the `decisions.jsonl` line above. The
legacy 3-file path still works via `signals.jsonl`:

```bash
grep '"snapshot_id":"YYYYMMDD_HHMM"' .run/strategy_app_historical/signals.jsonl \
  | grep '"signal_type":"HOLD"' \
  | jq '.reason'
```

The `reason` is one of (sorted by gate order — see [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md)):

| Reason | Meaning |
|---|---|
| `ml_pure_hold:post_stop_cooldown` | Previous trade STOP_LOSS'd; in cooldown |
| `ml_pure_hold:risk_breach_cooldown` | Previous RISK_BREACH; 5-bar cooldown |
| `ml_pure_hold:daily_soft_halt` | Day P&L < −20% threshold |
| `ml_pure_hold:soft_close_no_entry` | Past 15:00 IST |
| `ml_pure_hold:entry_below_threshold` | Model Stage-1 prob too low |
| `ml_pure_hold:direction_below_threshold` | Model Stage-2 prob too low |
| `ml_pure_hold:option_pnl_hold:prob_below_threshold:0.NNNN` | Bundle prob below 0.55 |
| `ml_pure_hold:missing_atm_or_strike_step_for_bundle` | Chain lookup failed for bundle strike |
| `ml_pure_hold:missing_option_premium` | Strike picked but no quote available |
| `ml_pure_hold:liquidity_gate_block` | Strike OI < 50k OR Vol < 15k |

### "What is the engine doing right now?"

```bash
cat .run/strategy_app_historical/runtime_state.json | jq '.last_event, .session'
```

`last_event` is the most recent action (entry / exit / hold) and its
`reason`. `session` has `hold_counts` (cumulative by reason for the day),
`bars_evaluated`, and the active `trade_date`.

### "What model is currently loaded?"

```bash
curl http://34.93.40.198:8008/api/strategy/current/state?mode=replay \
  | jq '.runtime_config | {engine, model_type, recipe_id, decision_threshold, model_run_id, model_package_path, checked_at_ist}'
```

This is the canonical answer for "what's deployed now" — read directly from
the engine's `runtime_state.json` via the dashboard API. The Diag tab
surfaces the same fields.

### "What was the P&L on trade X?"

```bash
grep '"position_id":"<short_uuid>"' .run/strategy_app_historical/positions.jsonl \
  | grep '"event":"POSITION_CLOSE"' \
  | jq '{pnl_pct, exit_reason, entry_premium, exit_premium, bars_held, reason}'
```

`reason` on the CLOSE event also has a human-readable summary
(`"TIME_STOP pnl=5.06% mfe=11.66% mae=-6.27% stop=0.00"`).

### "Did anything go wrong today? Halts, errors?"

```bash
# Daily soft halt triggered?
grep '"daily_soft_halt"' .run/strategy_app_historical/signals.jsonl | head -1

# How many HOLDs of each type today?
cat .run/strategy_app_historical/runtime_state.json | jq '.session.hold_counts'

# Container-level errors?
docker logs --since 1h option_trading-strategy_app_historical-1 2>&1 | grep -iE "error|exception|traceback"
```

### "How many trades today, what's the P&L?"

```bash
JSONL=.run/strategy_app_historical/positions.jsonl
TODAY=$(date +%Y%m%d)
grep "\"snapshot_id\":\"$TODAY" "$JSONL" | grep '"event":"POSITION_CLOSE"' \
  | jq -r '.pnl_pct' | awk '{s+=$1; n+=1; w+=($1>0)} END {print "trades="n, "net_pnl="s, "win_rate="w/n}'
```

### "Is the model's offline edge holding up in live?"

This is the question the audit harness answers. After a replay (or
periodically against live JSONL):

```bash
# Build per-trade parquet from the most recent run
python ml_pipeline_2/scripts/model_selection/audit_run.py \
  --trades <path/to/trades.parquet> \
  --return-col pnl_pct \
  --date-col trade_date \
  --output audit_today.json
cat audit_today.json | jq '.passed, .stats.t, .ci'
```

`passed: true` means current live trades still pass the same statistical
bar (t > 2 AND CI > 0 AND outlier survival) the offline holdout did.
Drift will show as `passed: false` over time.

### "What data is the model running on?"

The model bundle's `metadata.json` lists `feature_columns`. Cross-check
against the support dataset:

```bash
# bundle features
cat /app/.../<bundle>/feature_columns.json | jq '.feature_columns | length'

# v2 / v3 columns available
python -c "import pyarrow.parquet as pq; print(len(pq.read_schema(
  '/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2/year=2024/2024-10-31.parquet'
).names))"
```

Feature counts must match (modulo non-feature key columns).

---

## Log file layout (host paths on runtime VM)

```
/opt/option_trading/
├── .run/
│   ├── strategy_app/                  # LIVE container artifacts
│   │   ├── runtime_state.json
│   │   ├── signals.jsonl
│   │   ├── positions.jsonl
│   │   ├── decision_traces.jsonl
│   │   ├── metrics.jsonl
│   │   └── runtime_config.json
│   └── strategy_app_historical/       # HISTORICAL/REPLAY container
│       └── (same layout)
├── .backups/                          # mongodump + jsonl snapshots
│   └── cleanup_<TS>/
├── ml_pipeline_2/
│   ├── artifacts/
│   │   ├── option_pnl_bundles/        # deployed model bundles
│   │   ├── option_pnl_published_models/
│   │   ├── research/                  # training run outputs
│   │   ├── model_selection_runs/      # model-selection pipeline output
│   │   │   └── run_<YYYYMMDD>/
│   │   │       ├── state.json
│   │   │       ├── leaderboard.json
│   │   │       ├── leaderboard.md
│   │   │       └── cells/<cell_id>/
│   │   │           ├── train.log
│   │   │           ├── trades.parquet
│   │   │           └── audit.json
│   │   └── published_models/          # C1-family staged bundles
│   └── scripts/
│       └── model_selection/
│           ├── pipeline.py
│           ├── audit_run.py
│           ├── daemon.sh           # tmux/nohup wrapper
│           └── status.sh
└── docker-compose.yml
```

---

## Dashboard tabs and what they read

| Tab | Reads from | What it shows |
|---|---|---|
| LIVE | live `signals.jsonl`, current Diag API | Streaming live decisions |
| REPLAY | `signals.jsonl` + `positions.jsonl` for the selected historical date | Per-date trade list, chart, trade inspector |
| EVAL | `strategy_eval_runs` Mongo + JSONL | Past replay runs, trigger new ones |
| DIAG | `runtime_state.json` via `/api/strategy/current/state` | Currently-loaded model, recipe params, last event |

⚠ **Mongo can drift from JSONL** under persistence-queue overflow (see
[`runbooks/CLEANUP_ROLLBACK_RUNBOOK.md`](runbooks/CLEANUP_ROLLBACK_RUNBOOK.md)). When dashboard
counts disagree with `grep | wc -l` on JSONL, trust JSONL and check
the reconciliation script:
`/opt/option_trading/scripts/check_jsonl_mongo_reconciliation.sh`.

---

## Container health

```bash
# All container health states
sudo docker ps --format "{{.Names}}\t{{.Status}}"

# Strategy_app health endpoint
sudo docker exec option_trading-strategy_app_historical-1 python -m strategy_app.health

# Persistence app health (writes to mongo)
sudo docker logs --tail 5 option_trading-strategy_persistence_app_historical-1 \
  | grep "strategy persistence health"
```

The persistence app emits a periodic health line:

```
strategy persistence health consumed=N written=N ignored=0 errors=0 dropped=0 queue_depth=0 last_message_age_s=... last_flush_success_age_s=...
```

`dropped > 0` is the canary for Mongo not keeping up with JSONL — leads
to dashboard miscounts. The runbook covers how to handle.

---

## Known observability gaps (TODO)

These were identified during the 2026-05-19 cleanup. Each is a known gap
we should close opportunistically rather than at once.

1. ✅ **Per-snapshot `decisions.jsonl`** — *Closed (2026-06-04).* One
   always-on line per evaluate() call with input → what-happened → output /
   `blocking_gate`. Implemented in both `pure_ml_engine` and
   `deterministic_rule_engine`. See the "What happened at minute T?" recipe
   above. Collapsed "why didn't this fire" from a 3-file grep to one line.
2. **`/api/strategy/observability/summary`** — single JSON endpoint with
   deployed model, today's gate counts, recent trades, audit status.
   Lets the dashboard show a one-line health summary.
3. **Continuous runtime audit cron** — apply `audit_run.py` against a
   rolling 30-day window of live JSONL nightly. Detect edge decay
   without manual triggering.
4. **Standardised log format** — most components log structured JSON
   already, but a few legacy paths use bare `print()`. Incremental clean-up
   as we touch files.
5. **Trace IDs across layers** — currently each layer (snapshot, decision,
   trade, audit) has its own ID. End-to-end trace requires joining on
   `snapshot_id`. A flowing trace_id field would simplify root-cause
   diagnosis.

If you hit a question that doesn't have an answer in this guide, add it
to this list — that's the signal we need to close the gap.
