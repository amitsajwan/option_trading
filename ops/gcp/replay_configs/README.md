# Replay configs — experiment presets

Each `*.env` here is an experiment definition: a small env-file that **overlays**
`/opt/option_trading/.env.compose` for `strategy_app_historical` only.
Use them with `ops/gcp/run_replay.sh`.

## The full loop

```
┌─────────────────┐    daily auto         ┌─────────────────────────┐
│ Live capture    ├──────────────────────►│ Mongo: phase1_market_   │
│ (snapshot_app)  │  snapshots persisted  │   snapshots (live)      │
│ + depth_collect │                       │ + market_depth_ticks    │
└─────────────────┘                       └────────────┬────────────┘
                                                       │
                                                       │ promote_today_to_historical.py
                                                       ▼
                                          ┌─────────────────────────┐
                                          │ Mongo: phase1_market_   │
                                          │   snapshots_historical  │
                                          │   (replay corpus)       │
                                          └────────────┬────────────┘
                                                       │
                          ┌────────────────────────────┤
                          │                            │
                          ▼                            ▼
              ┌───────────────────┐         ┌─────────────────────┐
              │ ml_pipeline_2     │         │ run_replay.sh       │
              │ (train new model) │         │ (replay with preset)│
              │   bundle.joblib   │         │                     │
              └─────────┬─────────┘         │   POSTs eval API,   │
                        │                   │   pumps snapshots   │
                        │ reference path    │   through engine,   │
                        │  in preset env    │   tags with run_id  │
                        └──────────────────►│                     │
                                            └──────────┬──────────┘
                                                       │
                                                       ▼
                                          ┌─────────────────────────┐
                                          │ Dashboard REPLAY tab    │
                                          │   pick date + run_id    │
                                          │   pick UI speed         │
                                          │   watch it play out     │
                                          └─────────────────────────┘
```

## Promoting today's live data

```bash
# On the VM
cd /opt/option_trading
python3 ops/gcp/promote_today_to_historical.py             # today (IST)
python3 ops/gcp/promote_today_to_historical.py 2026-05-27  # specific date
```
Idempotent. Safe to re-run.

## Running a replay

```bash
# baseline (no env overrides — what live ran)
./ops/gcp/run_replay.sh 2026-05-27 baseline

# experiment: R1S without the leftover time-window gate
./ops/gcp/run_replay.sh 2026-05-27 r1s_no_time_window

# fastest possible (eval-side speed=0 = no throttle)
./ops/gcp/run_replay.sh 2026-05-27 baseline 0

# realistic playback (eval-side speed=1 second per snapshot)
./ops/gcp/run_replay.sh 2026-05-27 baseline 1
```
Eval-side `speed` controls the orchestrator's pump rate. The dashboard REPLAY
tab has its own independent UI speed control (`speed=4` default, configurable
per session).

## What's in each preset

- `baseline.env` — empty; uses .env.compose as-is. Control run.
- `r1s_no_time_window.env` — R1S profile with `ENTRY_TIME_WINDOWS=` (cleared).
  The fix from 2026-05-27 EOD; tests whether the 3 silently-killed afternoon
  candidates would have made it through.
- `custom_ml_bundle_example.env` — annotated template showing every model knob
  you can swap in: pure-ML staged bundle, direction-ML overlay, entry-ML
  overlay, option-PnL multi-bundle. Copy + edit to point at your new model.
- `buy_only_multi_model.env` — strict BUY-options preset (`debit_multi_v1`).
  Use this when you want no short-premium legs while still allowing optional
  entry/direction ML overlays and depth context.

## Training new models on the captured data

The live capture now persists fields that previous historical data does NOT
have: `payload.snapshot.nifty_context.*`, `payload.snapshot.underlying_context.*`,
`payload.snapshot.block_flow.*`, plus per-strike depth in `market_depth_ticks`.

To use these in a new model:

1. Promote enough live days into the historical collection (or export to parquet
   via `ops/gcp/run_snapshot_parquet_pipeline.sh`).
2. Update `ml_pipeline_2` feature spec to include the new fields.
3. Train via existing tournament/HPO workflow → produces a versioned bundle
   under `/opt/option_trading/ml_pipeline_2/artifacts/...`.
4. Copy `custom_ml_bundle_example.env` to a new preset name, fill in the
   bundle path, drop the explanatory comment.
5. `./run_replay.sh <date> <new_preset>` — first run against the same date as
   baseline so you can A/B directly via the dashboard EVAL tab (filter by
   run_id).

## Naming convention

`<profile-or-engine>_<one-line-hypothesis>.env`, e.g.
`r1s_with_depth_features.env`, `det_prod_lower_iv_ceiling.env`. Filename =
the headline of the experiment; the comment in the file explains the
hypothesis being tested.
