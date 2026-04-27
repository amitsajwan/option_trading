# strategy_app

Layer-4 strategy consumer runtime for snapshot events.

As-of: `2026-04-27`

## Purpose

- Subscribes to snapshot events from the Layer-3 topic.
- Calls the `StrategyEngine` contract on every snapshot.
- Classifies market regime before choosing which strategies run.
- Handles session lifecycle: `on_session_start(date)`, `evaluate(snapshot)`, `on_session_end(date)`.

## Engine Lanes

| Lane | Purpose |
|---|---|
| `deterministic` | Research and replay. Rule-based regime routing. |
| `ml_pure` | Live production. 3-stage ML inference on every snapshot. |

The legacy transitional wrapper and registry-backed `ml_entry` overlay are removed.

## Contract

`strategy_app/contracts.py` defines:

- `StrategyEngine` — ABC consumed by the Redis event loop
- `TradeSignal` — final action emitted downstream
- `StrategyVote` — per-strategy candidate vote (deterministic lane)
- `PositionContext`, `RiskContext` — state passed to strategies

## Run

```bash
python -m strategy_app.main --engine deterministic
python -m strategy_app.main --engine ml_pure \
  --ml-pure-model-package <path-or-gs-url> \
  --ml-pure-threshold-report <path-or-gs-url>
```

Full CLI reference is in `strategy_app/main.py`. Key arguments:

| Argument | Env var | Default | Notes |
|---|---|---|---|
| `--engine` | `STRATEGY_ENGINE` | `deterministic` | `deterministic` or `ml_pure` |
| `--topic` | — | `snapshot_topic()` | Override Redis topic |
| `--min-confidence` | — | `0.65` | Entry gate for deterministic lane |
| `--max-events` | — | `0` (infinite) | Stop after N events |
| `--ml-pure-model-package` | `ML_PURE_MODEL_PACKAGE` | — | Path or `gs://` URL to `model.joblib` |
| `--ml-pure-threshold-report` | `ML_PURE_THRESHOLD_REPORT` | — | Path or `gs://` URL to `threshold_report.json` |
| `--ml-pure-run-id` | `ML_PURE_RUN_ID` | — | Auto-resolve artifacts by run-id (strict) |
| `--ml-pure-model-group` | `ML_PURE_MODEL_GROUP` | — | Used with `--ml-pure-run-id` |
| `--ml-pure-max-feature-age-sec` | `ML_PURE_MAX_FEATURE_AGE_SEC` | `90` | Staleness gate |
| `--ml-pure-max-nan-features` | `ML_PURE_MAX_NAN_FEATURES` | `3` | NaN tolerance before hold |
| `--ml-pure-max-hold-bars` | `ML_PURE_MAX_HOLD_BARS` | `15` | Time-stop fallback |
| `--ml-pure-min-oi` | `ML_PURE_MIN_OI` | `50000.0` | Liquidity gate |
| `--ml-pure-min-volume` | `ML_PURE_MIN_VOLUME` | `15000.0` | Liquidity gate |
| `--rollout-stage` | — | `paper` | `paper`, `shadow`, or `capped_live` |
| `--position-size-multiplier` | — | `1.0` | Must be `<= 0.25` for `capped_live` |
| `--strategy-profile-id` | `STRATEGY_PROFILE_ID` | `det_prod_v1` (det) | Profile tag for replay comparability |
| `--ml-runtime-guard-file` | `ML_RUNTIME_GUARD_FILE` | — | JSON approval artifact required for live ML |

## ml_pure: Explicit Path Mode

Pass model and threshold paths directly. Both accept local paths or `gs://` URLs:

```bash
python -m strategy_app.main \
  --engine ml_pure \
  --ml-pure-model-package gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/model/model.joblib \
  --ml-pure-threshold-report gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/config/profiles/ml_pure_staged_v1/threshold_report.json
```

GCS files are downloaded on first use and cached locally. Cache location:

- `GCS_ARTIFACT_CACHE_DIR` env var (default: `~/.cache/option_trading_models/`)
- Requires `pip install google-cloud-storage` and active GCP credentials.

## ml_pure: Run-ID Mode

Auto-resolve artifacts from the published training report:

```bash
python -m strategy_app.main \
  --engine ml_pure \
  --ml-pure-run-id 20260308_164057 \
  --ml-pure-model-group banknifty_futures/h15_tp_auto
```

This reads from:

```
ml_pipeline_2/artifacts/published_models/<model_group>/reports/training/run_<run_id>.json
```

Strict checks applied:
- `publish_decision.decision` must be `PUBLISH` or `publish_status` must be `published`
- `published_paths.model_package` must exist
- `published_paths.threshold_report` must exist

Do not mix run-id mode and explicit path mode in the same invocation.

## ml_pure: Runtime Guard

Live ML (`capped_live` stage) requires a guard file:

```bash
python -m strategy_app.main \
  --engine ml_pure \
  --rollout-stage capped_live \
  --position-size-multiplier 0.20 \
  --ml-runtime-guard-file /path/to/guard.json \
  --ml-pure-model-package <path> \
  --ml-pure-threshold-report <path>
```

Guard file must contain:
- `approved_for_runtime: true`
- `offline_strict_positive_passed: true`
- `paper_days_observed >= 10`
- `shadow_days_observed >= 10`

## deterministic: Common Options

Tune confidence gate:

```bash
python -m strategy_app.main --engine deterministic --min-confidence 0.70
```

Use historical replay topic:

```bash
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical
```

Consume only first 100 events:

```bash
python -m strategy_app.main --engine deterministic --max-events 100
```

Risk profile shortcut:

```bash
RISK_PROFILE=aggressive_safe_v1 python -m strategy_app.main --engine deterministic
```

`aggressive_safe_v1` defaults:

| Env var | Value |
|---|---|
| `RISK_LOT_SIZING_MODE` | `budget_per_trade` |
| `RISK_NOTIONAL_PER_TRADE` | `50000` |
| `RISK_MAX_DAILY_LOSS_PCT` | `0.02` |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` |
| `RISK_MAX_LOTS_PER_TRADE` | `20` |

Any explicit `RISK_*` env var overrides the profile default.

## Compose

Set in `.env.compose`:

```
STRATEGY_ENGINE=ml_pure
# Choose one of:
ML_PURE_RUN_ID=<run_id>
ML_PURE_MODEL_GROUP=<model_group>
# or:
ML_PURE_MODEL_PACKAGE=<local-path-or-gs-url>
ML_PURE_THRESHOLD_REPORT=<local-path-or-gs-url>
```

If PowerShell shell vars are leaking into compose, clear them:

```powershell
Remove-Item Env:ML_PURE_RUN_ID -ErrorAction SilentlyContinue
Remove-Item Env:ML_PURE_MODEL_GROUP -ErrorAction SilentlyContinue
```

Compose startup command resolves to:

```bash
python -m strategy_app.main --engine ${STRATEGY_ENGINE} --topic market:snapshot:v1 --min-confidence 0.65
```

## Regime Chain

| Regime | Condition | Routing |
|---|---|---|
| `AVOID` | VIX spike or pre-close | No new entries |
| `HIGH_VOL` | Elevated realized vol + elevated VIX | `IV_FILTER + HIGH_VOL_ORB` |
| `EXPIRY` | Expiry day | `IV_FILTER + VWAP_RECLAIM` |
| `PRE_EXPIRY` | One day before expiry | Conservative ORB + OI |
| `TRENDING` | ORB, OI buildup, prev-day level breakout | `ORB`, `OI_BUILDUP`, `EMA_CROSSOVER`, `PREV_DAY_LEVEL` |
| `SIDEWAYS` | VWAP conditions | `VWAP_RECLAIM`, `OI_BUILDUP` |

`EXPIRY_MAX_PAIN` is not enabled in the default routing.

## Deterministic Exit Priority

Hard exits (stop/trail/time/risk) fire first on every bar. Strategy exits then resolve:

1. Owner strategy exit
2. Configured helper exit
3. Tracker/risk universal mechanics

`EMA_CROSSOVER` is not in the default universal exit candidate set (default fallback: `ORB`, `VWAP_RECLAIM`, `OI_BUILDUP`).

## Engine-Aware Signal Fields

Both engines annotate every signal and vote with:

| Field | Values |
|---|---|
| `engine_mode` | `deterministic` or `ml_pure` |
| `decision_mode` | `rule_vote` or `ml_staged` |
| `decision_reason_code` | Normalized code (`below_threshold`, `feature_stale`, etc.) |
| `decision_metrics` | Optional payload (`ce_prob`, `pe_prob`, thresholds, edge, confidence) |
| `strategy_family_version` | `DET_V1` or `ML_PURE_STAGED_V1` |
| `strategy_profile_id` | `det_prod_v1` (deterministic default), `ml_pure_staged_v1` (ml_pure default) |

Position lifecycle rows preserve:

- `signal_id` — entry signal id, unchanged across `POSITION_OPEN`, `POSITION_MANAGE`, `POSITION_CLOSE`
- `snapshot_id` — snapshot that triggered the lifecycle event
- `entry_snapshot_id` — original open snapshot on all lifecycle rows

Risk sizing: `RISK_NOTIONAL_PER_TRADE` and `RISK_PER_TRADE_PCT` are hard caps. `RISK_CONFIDENCE_FLOOR` scales allocation down only; it does not boost sizing above the cap.

## Modularization

Logging paths:

- `strategy_app/logging/decision_field_resolver.py`
- `strategy_app/logging/jsonl_sink.py`
- `strategy_app/logging/redis_event_publisher.py`
- `strategy_app/logging/signal_logger.py` — public entrypoint

Engine annotation:

- `strategy_app/engines/decision_annotation.py` — shared by both lanes

## Container

Build:

```bash
docker build -f strategy_app/Dockerfile -t strategy_app:local .
```

Health check:

```bash
python -m strategy_app.health
```

## Related Docs

- [STRATEGY_ML_FLOW.md](STRATEGY_ML_FLOW.md)
- [OPERATOR_PLAYBOOK.md](OPERATOR_PLAYBOOK.md)
- [RELEASE_READINESS_CHECKLIST.md](RELEASE_READINESS_CHECKLIST.md)
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)
- [strategy_catalog.md](strategy_catalog.md)
- [ENGINE_CONSOLIDATION_PLAN.md](ENGINE_CONSOLIDATION_PLAN.md)
