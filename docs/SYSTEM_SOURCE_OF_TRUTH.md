# BankNifty System Source Of Truth

As-of date: `2026-04-27`

If active docs conflict with code, code wins. If active docs conflict with each other, this file wins.

## 1. Runtime Contract

- Live trading runtime is `strategy_app.main --engine ml_pure`.
- Deterministic runtime is retained for replay and research only.
- Legacy transitional runtime wrapper and registry-backed ML entry overlay have been removed.
- `ml_pipeline_2` is the only supported ML training and publish source for live runtime artifacts.
- Event bus is Redis and persistence is MongoDB.

## 2. ML Artifact Contract

- Runtime artifact selection is by `ML_PURE_RUN_ID` + `ML_PURE_MODEL_GROUP`, or explicit package/report paths.
- Explicit paths (`ML_PURE_MODEL_PACKAGE`, `ML_PURE_THRESHOLD_REPORT`) accept local file paths or `gs://` GCS URLs. GCS files are downloaded to local cache on first use (`GCS_ARTIFACT_CACHE_DIR`, default `~/.cache/option_trading_models/`).
- In run-id mode, startup is blocked unless publish validation passes and both resolved artifact paths exist.
- `runtime.block_expiry` must stay aligned between training and live runtime policy.
- Active staged label generation is built from forward futures-path barrier labeling in `ml_pipeline_2/labeling/engine.py`, not from deterministic strategy replay exits.

**Current research model (not production-ready):**

- Run: `staged_simple_s2_v1_20260426_110326`
- GCS root: `gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/`
- Status: research checkpoint — all production gates failed — `paper` rollout stage only
- Details: `ml_pipeline_2/docs/MODEL_STATE_20260426.md`

## 3. Runtime Guard Contract

When `engine=ml_pure` with a model package set (`runtime_ml_enabled=true`):

- `paper` and `shadow` stages: allowed without guard file — no real capital is sized.
- `capped_live` stage: full guard required:
  - `position_size_multiplier <= 0.25`
  - guard file (`STRATEGY_ML_RUNTIME_GUARD_FILE`) must confirm:
    - `approved_for_runtime=true`
    - `offline_strict_positive_passed=true`
    - `paper_days_observed>=10`
    - `shadow_days_observed>=10`
- Any other stage value raises a startup error.

## 4. Current Research + Replay Contract

- Deterministic replay validates B1-B5 behavior and exit attribution.
- After replay validation, decide whether staged views, label recipes, or training windows need regeneration before retraining `ml_pipeline_2`.
- The supported training and publish entrypoint is `python -m ml_pipeline_2.run_staged_release ...`.
- `python -m ml_pipeline_2.run_research ...` remains supported for manifest validation and research runs, but it does not publish a live runtime handoff by itself.
- There is no supported runtime ML overlay on top of deterministic votes.

## 5. Canonical References

- **Zero-to-live setup:** `docs/runbooks/LIVE_SETUP_GUIDE.md`
- Runbooks index: `docs/runbooks/README.md`
- Snapshot workflow: `docs/runbooks/GCP_SNAPSHOT_PARQUET_RUN_GUIDE.md`
- Training workflow: `docs/runbooks/TRAINING_RELEASE_RUNBOOK.md`
- Live runtime workflow: `docs/runbooks/GCP_DEPLOYMENT.md`
- Cleanup workflow: `docs/runbooks/CLEANUP_ROLLBACK_RUNBOOK.md`
- Runtime flow: `strategy_app/docs/STRATEGY_ML_FLOW.md`
- Strategy current-state validation: `strategy_app/docs/CURRENT_TREE_VALIDATION.md`
- Strategy catalog: `strategy_app/docs/strategy_catalog.md`
- Module design: `strategy_app/docs/detailed-design.md`
- Consolidation status: `strategy_app/docs/ENGINE_CONSOLIDATION_PLAN.md`
- Module-level staged ML detail: `ml_pipeline_2/docs/gcp_user_guide.md`

## 6. Last Verified Commands

```bash
python -m snapshot_app.historical.snapshot_batch_runner --validate-only --base .data/ml_pipeline/parquet_data --window-manifest-out .run/window_manifest_latest.json
python -m ml_pipeline_2.run_research --config ml_pipeline_2/configs/research/staged_dual_recipe.default.json --validate-only
python -m strategy_app.main --engine deterministic --topic market:snapshot:v1:historical
python -m strategy_app.main --engine ml_pure --ml-pure-run-id <run_id> --ml-pure-model-group banknifty_futures/h15_tp_auto
# GCS explicit path mode (new as of 2026-04-27):
python -m strategy_app.main --engine ml_pure \
  --ml-pure-model-package gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/model/model.joblib \
  --ml-pure-threshold-report gs://amittrading-493606-option-trading-models/published_models/research/staged_simple_s2_v1/config/profiles/ml_pure_staged_v1/threshold_report.json
```

**GCP deploy:**
```bash
gcloud compute ssh savitasajwan03@option-trading-runtime-01 --zone asia-south1-b --project amittrading-493606
cd /opt/option_trading && bash ./ops/gcp/runtime_lifecycle_interactive.sh
```
