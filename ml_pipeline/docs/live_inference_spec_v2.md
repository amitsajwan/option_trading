# Live Inference Spec V2 (T22): Exit-Aware Paper Mode

T22 extends live inference with exit-aware replay mode and position state persistence.

## New Replay Mode

`run_mode = replay-dry-run-v2`

Behavior:

1. Emits `ENTRY` events on buy signals.
2. Maintains one open paper position at a time.
3. Emits `MANAGE` events while holding.
4. Emits `EXIT` events on:
   - `signal_flip`
   - `time_stop`
   - `confidence_fade`
   - `session_end` (forced close at replay end)

## Key Controls

- `max_hold_minutes`
- `confidence_buffer`

## Live API Exit-Aware Mode

`run_mode = live-api-v2`

Behavior:

1. Polls live runtime APIs for latest feature row.
2. Emits one event per new timestamp with persistent position state.
3. Produces `ENTRY|MANAGE|EXIT|IDLE` events using the same state machine as replay-v2.
4. Emits a `session_end` exit when loop stops with open position.

## Output Contract

JSONL events include:

- model probabilities/thresholds
- `event_type` (`ENTRY|MANAGE|EXIT|IDLE`)
- `event_reason`
- `position` snapshot
- `held_minutes` for managed/exited positions

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode replay-dry-run-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --feature-parquet ml_pipeline\artifacts\t04_features.parquet --output-jsonl ml_pipeline\artifacts\t22_exit_aware_paper_events.jsonl --limit 300 --max-hold-minutes 5 --confidence-buffer 0.05
```

Live API variant:

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-api-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --market-api-base http://127.0.0.1:8004 --dashboard-api-base http://127.0.0.1:8002 --output-jsonl ml_pipeline\artifacts\t30_live_api_v2_events.jsonl --poll-seconds 2 --max-iterations 40 --max-hold-minutes 5 --confidence-buffer 0.05
```

Pure Redis pub/sub variant (no REST calls):

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.live_inference_adapter --run-mode live-redis-v2 --mode dual --model-package ml_pipeline\artifacts\t06_baseline_model.joblib --threshold-report ml_pipeline\artifacts\t08_threshold_report.json --instrument BANKNIFTY-I --redis-host localhost --redis-port 6380 --redis-db 0 --depth-channel market:depth:BANKNIFTY-I --output-jsonl ml_pipeline\artifacts\t30_live_redis_v2_events.jsonl --max-iterations 200 --max-hold-minutes 5 --confidence-buffer 0.05 --max-idle-seconds 120
```

Depth enrichment:

- Every `live-redis-v2` event includes a `depth` object with top-of-book and imbalance fields.
- Source is Redis-first: depth pub/sub (`market:depth:{instrument}`) when present, with Redis depth key fallback (`depth:{instrument}:*`).

## Artifact

- `ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl`
- `ml_pipeline/artifacts/t30_live_api_v2_events.jsonl` (runtime dependent)
- `ml_pipeline/artifacts/t30_live_redis_v2_events.jsonl` (runtime dependent)
