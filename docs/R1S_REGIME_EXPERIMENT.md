# R1S daily regime experiment log

**Status:** in progress (2026-05-20)  
**Hypothesis:** multi-day regime features separate R1S PASS quarters (calm bullish drift) from FAIL- quarters (macro vol).

See also [PROJECT_STATUS_2026-05-20.md](PROJECT_STATUS_2026-05-20.md) §6–7.

---

## Features (offline → flat v3)

| Column | Definition | Lookahead |
|--------|------------|-----------|
| `regime_rv20` | 20d std of daily futures returns × √252 | shift(1) |
| `regime_dist_sma20` | (close − SMA20) / SMA20 | shift(1) |
| `regime_sma20_slope` | 5d pct_change of SMA20 | shift(1) |
| `regime_60d_return` | close / close_60d_ago − 1 | shift(1) |

**Builder:** `ml_pipeline_2/scripts/feature_builder/build_daily_regime_v3.py`  
**Core logic:** `ml_pipeline_2/scripts/feature_builder/regime_daily.py`

### India VIX (prior-day gate)

| Column | Definition |
|--------|------------|
| `regime_vix_close` | Prior trading day India VIX close (shift 1) |
| `regime_vix_high` | 1 if `regime_vix_close >= 20` (same threshold as `ctx_is_high_vix_day`) |

**Ingest** from NSE hist CSVs (`C:\code\banknifty_raw\banknifty_data\vix` — include full-year files e.g. `hist_india_vix_-01-01-2024-to-31-12-2024.csv`; partial 2024 file is redundant after full-year is added):

```powershell
python -m ml_pipeline_2.scripts.feature_builder.ingest_india_vix `
  --vix-root C:/code/banknifty_raw/banknifty_data/vix `
  --parquet-root .data/ml_pipeline/parquet_data --force

# Then re-backfill regime columns (includes VIX when vix.parquet exists):
python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_v3 `
  --start 2020-08-03 --end 2024-10-31 `
  --parquet-root .data/ml_pipeline/parquet_data
```

Or one step: `build_daily_regime_v3 --ingest-vix --vix-root ... --start ... --end ...`

---

## VM commands (ML: `option-trading-ml-01`)

```bash
cd ~/option-trading-ml
git pull --ff-only

# 1) Backfill regime columns onto flat v3
python -m ml_pipeline_2.scripts.feature_builder.build_daily_regime_v3 \
  --start 2020-08-03 --end 2024-10-31

# 2) Diagnostic: PASS vs FAIL- feature separation
python -m ml_pipeline_2.scripts.rules_pipeline.diagnose_regime_features

# 3) Threshold grid (day-level block rates)
python -m ml_pipeline_2.scripts.rules_pipeline.sweep_regime_thresholds

# 4) 17Q + dual-window audit sweep
python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_r1s_regime_v1_history.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/r1s_regime_v1_$(date +%Y%m%d)

cat ml_pipeline_2/artifacts/rules_runs/r1s_regime_v1_*/leaderboard.md
```

---

## Rule variant

**Config:** [r1s_short_ce_orbdown_regime_v1.json](../ml_pipeline_2/configs/rules/r1s_short_ce_orbdown_regime_v1.json)

Disqualifiers (OR) in addition to R1S baseline:

- `regime_rv20 > 0.20` (annualized; tune after sweep)
- `regime_sma20_slope <= 0`

---

## Go / no-go (fill after VM run)

| Criterion | Baseline R1S | R1S_REGIME_V1 | Met? |
|-----------|--------------|---------------|------|
| 17Q PASS count | 6 / 17 | _TBD_ | |
| FAIL- quarters mostly blocked / fail audit | — | _TBD_ | |
| May–Jul 2024 + Aug–Oct 2024 both PASS | split | _TBD_ | |
| PASS quarters ≥30 trades, net w/o top-5 ≥ 0 | yes | _TBD_ | |

**GO** → wire rules engine to `strategy_app` for paper.  
**NO-GO** → manual weekly gating or stop automated R1S.

---

## Results (2026-05-20 VM)

### Backfill

`build_daily_regime_v3` on ML VM: **1053 OK**, 498 skipped (non-trading days).

### Diagnostic (`diagnose_regime_features`)

Daily regime **window medians** (PASS vs FAIL-):

| Feature | PASS | FAIL- |
|---------|------|-------|
| `regime_rv20` | 0.193 | 0.145 |
| `regime_sma20_slope` | 0.0049 | 0.0018 |
| `regime_60d_return` | 0.051 | 0.023 |

**Finding:** `regime_rv20` points the **wrong way** for a high-vol disqualifier (PASS quarters are *higher* rv20). Do not use `regime_rv20 > X` as primary gate.

### Day-level block sweep (`sweep_regime_thresholds`, rv20 + slope>0)

| rv_max | pass_block% | failm_block% |
|--------|-------------|--------------|
| 0.20 | 64.5 | 47.0 |
| 0.26 | 56.3 | 40.8 |

Blocks more PASS trading days than FAIL- at all thresholds tested.

### Rule variants

| Config | Disqualifiers |
|--------|----------------|
| `r1s_short_ce_orbdown_regime_v1.json` | rv20>0.20, slope<=0 (likely wrong — see above) |
| `r1s_short_ce_orbdown_regime_v2.json` | slope<=0, 60d_return<=0 (5/19 PASS — NO-GO) |
| `r1s_short_ce_orbdown_regime_v3.json` | `regime_vix_high==1`, slope<=0 — **2/19 PASS, NO-GO** (worse than baseline 6/17) |

### 17Q audit sweep

```bash
# v2 (recommended)
python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_r1s_regime_v2_history.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/r1s_regime_v2_$(date +%Y%m%d)
```

### R1S_REGIME_V2 sweep (`r1s_regime_v2_20260520`)

**5 PASS / 14 FAIL** (19 cells: 17Q + May–Jul + Aug–Oct).

| vs baseline R1S (6/17) | v2 result |
|------------------------|-----------|
| PASS quarters kept | 2020_aug_dec, 2021_q1, 2021_q4, 2024_q2 (+ 2024_may_jul) |
| Lost vs baseline PASS | 2023_q3, 2024_q1 |
| Canonical dual-window | **May–Jul PASS**, **Aug–Oct FAIL** (t=+0.14) |

**Verdict: NO-GO** for automated deploy — filter does not improve quarter PASS rate and still fails Aug–Oct 2024. Next: manual regime gating or new features (e.g. India VIX level), not runtime wiring of v2.

### R1S_REGIME_V3 sweep (`r1s_regime_v3_20260520`, India VIX + slope)

**2 PASS / 17 FAIL** — worse than unfiltered R1S (6/17) and v2 (5/19).

| Window | Result |
|--------|--------|
| May–Jul 2024 | PASS (t=+2.27) |
| Aug–Oct 2024 | FAIL (t=+0.18) |
| Lost vs baseline | 2020_aug_dec, 2021_q4, 2023_q3, 2024_q1/q2 among former PASS quarters |

`regime_vix_high` + positive slope **over-filters** calm quarters (2021 Q1 still PASS; many others fail). Real India VIX did not fix the regime problem for automated gating.
