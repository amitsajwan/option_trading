# Clean Slate Rebaseline Runbook

## Scope
This runbook is for rebuilding deterministic + ML from scratch after clearing old evaluation artifacts.

## What was reset
- Cleared old evaluation/training outputs under `.run/` (open-search, revalidated, legacy EQ runs, old model dumps).
- Kept live/runtime folders: `.run/snapshot_app`, `.run/strategy_app`, `.run/strategy_app_historical`, `.run/ingestion_app`.
- Disabled stale ML runtime override in `.env.compose`:
  - `STRATEGY_ML_ENTRY_REGISTRY=`
  - `STRATEGY_ML_ENTRY_EXPERIMENT_ID=`

## Canonical manifests
- Latest contiguous block (global): `.run/window_manifest/latest_window_manifest.json`
  - Current status: exploratory-only (`formal_ready=false`).
- Current formal execution manifest: `.run/window_manifest/window_20210101_20220217_v2_verified.json`
  - Current status: formal-ready (`trading_days=281`).
- 2020 block: `.run/window_manifest/window_2020_v2_contiguous.json`
  - Current status: exploratory-only (`trading_days=121`).

## Current checkpoint (March 5, 2026)
- Deterministic formal run completed:
  - `.run/deterministic_open_matrix_20210101_20220217_medium_strict/champion.json`
  - champion: `risk_medium_20_trail_regime_default_set_trend_core`
  - holdout return: `+1.3623%`, max drawdown: `-1.4675%`, trades: `67`
- Deterministic 2020 exploratory robustness run completed:
  - `.run/deterministic_open_matrix_2020_exploratory_medium/champion.json`
  - champion: `risk_medium_20_trail_regime_default_set_all`
  - holdout return: `+0.5028%`, max drawdown: `-0.3949%`, trades: `14`
- Full formal open-search cycle is running:
  - root: `.run/open_search_rebaseline_20260305_main/2021-01-01_2022-02-17_5e931d3969`
  - training complete (`trained_variant_count=720`)
  - replay/gating/champion stages pending completion

## First clean-slate benchmark (already executed)
- Output root: `.run/open_search_rebaseline_clean`
- Cycle ID: `2021-01-01_2021-12-31_74d964e5ba`
- Command used:
```powershell
python -m strategy_app.tools.open_search_rebaseline_cycle `
  --window-manifest .run/window_manifest/window_2021_v2_contiguous.json `
  --formal-run `
  --parquet-base ml_pipeline/artifacts/data/parquet_data `
  --output-root .run/open_search_rebaseline_clean `
  --deterministic-search-spec .run/deterministic_search_spec_smoke.json `
  --feature-profiles eq_full_v1 `
  --label-profiles mfe15_gt_5_v1 `
  --segmentation-policies seg_regime_v1 `
  --model-families logreg_baseline_v1 `
  --threshold-policies fixed_060,segment_optimal,strategy_override_v1 `
  --max-champions 3
```

## Tracking results
Use one command after every cycle:
```powershell
python -m strategy_app.tools.rebaseline_results_tracker `
  --runs-root .run/open_search_rebaseline_clean `
  --out-csv .run/rebaseline_tracker/results.csv `
  --out-json .run/rebaseline_tracker/results.json
```

## One-by-one execution order
1. Deterministic smoke + single ML family (`logreg_baseline_v1`) on 2021 formal window.
2. Add `lgbm_default_v1`, rerun same window, compare tracker row-to-row.
3. Add `lgbm_regularized_v1`, rerun same window.
4. Expand feature profiles (`eq_core_snapshot_v1` + `eq_full_v1`) only after step 3 is stable.
5. Only then run larger matrix and exception policy checks.

## Guardrails
- Keep each cycle on one frozen manifest.
- Compare only through tracker output (`results.csv`), not ad-hoc logs.
- Do not re-enable ML runtime in `.env.compose` until hard-gate champion appears.
