# Fixed-Strike Remediation Checklist

Historical note:

- this checklist is preserved for incident history and fixed-strike remediation context
- commands that invoke removed `ml_pipeline` modules are no longer runnable on the current branch
- use current `snapshot_app`, `strategy_app`, and `ml_pipeline_2` docs for active operator workflows

Run from repo root: repo checkout root

## A. Pre-Flight

- [ ] Confirm no conflicting snapshot batch job is writing the same base path.
- [ ] Confirm schema baseline is `2.0` in snapshot builder and runner.
- [ ] Confirm runner rebuild default fields are structural-only (`strike_count`).

## B. Rebuild Production Snapshot Base

```powershell
python -m snapshot_app.historical.snapshot_batch_runner `
  --base .data/ml_pipeline/parquet_data `
  --rebuild-missing-fields `
  --log-every 10 `
  --write-batch-days 20 `
  --print-iv-diagnostics
```

Record output:

- [ ] `required_version`
- [ ] `missing_field_days`
- [ ] `version_mismatch_days`
- [ ] `days_processed`
- [ ] `error_days`
- [ ] `elapsed_sec`
- [ ] `iv_diagnostics`
- [ ] `iv_diagnostics_days_with_failures`

Validation:

```powershell
python -m snapshot_app.historical.snapshot_batch_runner `
  --base .data/ml_pipeline/parquet_data `
  --validate-only `
  --validate-days 20
```

- [ ] Validation run completed.

## C. Build Fixed-Strike Canonical Root

Create canonical root:

- [ ] `.run/canonical_eq_e2e_fixed_strike_20200101_20230928`

Copy snapshots into required layout:

```powershell
robocopy .data/ml_pipeline/parquet_data/snapshots `
  .run/canonical_eq_e2e_fixed_strike_20200101_20230928/snapshots `
  /E
```

- [ ] `snapshots/year=*/data.parquet` present under fixed root.

Copy experiment registry into fixed root:

```powershell
New-Item -ItemType Directory -Force `
  .run/canonical_eq_e2e_fixed_strike_20200101_20230928/models | Out-Null
Copy-Item `
  .run/canonical_eq_e2e_expanded_20230928/models/experiment_registry.csv `
  .run/canonical_eq_e2e_fixed_strike_20200101_20230928/models/experiment_registry.csv `
  -Force
```

- [ ] `models/experiment_registry.csv` present under fixed root.

## D. Replay Eval + Champion

Removed on current branch:

- the legacy `ml_pipeline` replay-eval and champion-select modules referenced below were part of the retired package
- keep this section as historical evidence only

Replay eval:

```powershell
python -m ml_pipeline.entry_quality_replay_eval `
  --registry .run/canonical_eq_e2e_fixed_strike_20200101_20230928/models/experiment_registry.csv `
  --parquet-base .run/canonical_eq_e2e_fixed_strike_20200101_20230928 `
  --start-date 2020-01-01 `
  --end-date 2023-09-28 `
  --capital 500000 `
  --top-k 10 `
  --output-dir .run/canonical_eq_e2e_fixed_strike_20200101_20230928/eval_frozen
```

Champion select:

```powershell
python -m ml_pipeline.entry_quality_champion_select `
  --evaluation-registry .run/canonical_eq_e2e_fixed_strike_20200101_20230928/eval_frozen/evaluation_registry.csv `
  --output-dir .run/canonical_eq_e2e_fixed_strike_20200101_20230928/champions `
  --max-champions 5 `
  --min-trades 10 `
  --max-drawdown-pct -0.50 `
  --drawdown-multiple 1.15 `
  --min-trade-ratio 0.60 `
  --max-single-strategy-return-share 0.70
```

- [ ] `eval_frozen/evaluation_registry.csv` exists.
- [ ] `champions/champion_registry.csv` exists.
- [ ] `champions/champion_registry.json` exists.

## E. Technical Validation

```powershell
python -m pytest strategy_app/tests/test_position_risk.py -q
python -m pytest snapshot_app/tests/test_snapshot_ml_flat_contract_runtime.py -q
```

- [ ] Fixed-strike regression tests pass.
- [ ] Snapshot contract tests pass.
- [ ] No strike-to-ATM repricing drift in audit sample.
- [ ] Live-vs-historical schema compare is clean after deep flatten fix.

## F. Sign-Off

- [ ] Architect sign-off captured.
- [ ] Trader sign-off captured.
- [ ] Data scientist sign-off captured.
- [ ] Sign-off manifest written:
  - `docs/manifests/fixed_strike_signoff_YYYYMMDD.json`

## G. Superseded Run Deletion (Post Sign-Off Only)

- [ ] Pre-delete manifest written:
  - `docs/manifests/fixed_strike_delete_manifest_YYYYMMDD_HHMMSS.json`
- [ ] Delete all `.run/canonical_*` except:
  - `.run/canonical_eq_e2e_fixed_strike_20200101_20230928`
- [ ] Verify only fixed-strike canonical root remains.

## H. Documentation Promotion

- [ ] `docs/SUPPORT_BRINGUP_GUIDE.md` updated with fixed-strike canonical paths and contract notes.
- [ ] `docs/SYSTEM_SOURCE_OF_TRUTH.md` updated with fixed-strike canonical paths and superseded ATM-path status.
- [ ] Strict nullable OHLC contract (`no fallback`) explicitly documented.
