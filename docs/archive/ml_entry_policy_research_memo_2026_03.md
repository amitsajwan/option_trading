# ML Entry Policy Research Memo

Full-history and holdout results · Phase 1 recommendation  
March 2026 · Confidential · Internal research record

## Purpose

This memo records the findings from the asymmetric-threshold ML entry policy experiment.
It documents what was found, what was not found, and why the next action is data expansion
rather than further threshold tuning on the current dataset.

## 1. Context

The deterministic strategy system was producing a `-6.42%` capital return on 210 trading days.
`EXPIRY_MAX_PAIN` accounted for nearly all of that loss. Once removed, the remaining stack
(`OI_BUILDUP`, `ORB`, `EMA_CROSSOVER`, `VWAP_RECLAIM`, `PREV_DAY_LEVEL`) was approximately
flat at `-0.09%`, but with `EMA_CROSSOVER` as a consistent drag and `OI_BUILDUP` as the only
strategy with reliable positive signal.

The ML work was initiated because the deterministic entry policy (`LongOptionEntryPolicy`)
applied independent rule thresholds to each feature and could not express the interactions
that matter for option buying quality. A segmented LightGBM model was trained on a
candidate-vote dataset of 14,166 rows, with one row per directional entry vote generated
from replay, not just executed trades, to avoid selection bias.

What was built:

- canonical candidate generation now lives in `ml_pipeline.entry_candidate_dataset`
- canonical experiment training now lives in `ml_pipeline.entry_quality_experiments`
- `entry_candidate_labels.parquet` - 14,166 rows, about `50.8%` positive label balance
- `entry_quality_segmented_bundle.joblib` - three models: `TRENDING`, `PRE_EXPIRY`, `SIDEWAYS`
- `ml_entry_policy.py` - `MLEntryPolicy` with strategy-aware thresholds per regime
- `offline_strategy_analysis.py` - wired with ML entry-policy flags for A/B replay

Historical note:

- earlier ad hoc research scripts under `label_generators/` were removed after the canonical
  `ml_pipeline` path reached FE parity and became the supported workflow

## 2. Model Baseline

Segmented model results used a time split:

- train: `2020-01-01` to `2022-12-31`
- validation: `2023-01-01` to `2023-12-31`
- eval: `2024-01-01` to `2024-02-09`

| Segment | Val ROC AUC | Eval ROC AUC | Heuristic Baseline | Calibration |
| --- | ---: | ---: | ---: | --- |
| `TRENDING` | ~0.71 | ~0.622 | 0.493 | Isotonic |
| `PRE_EXPIRY` | ~0.71 | ~0.602 | 0.493 | Isotonic |
| `SIDEWAYS` | ~0.71 | ~0.680 | N/A | Isotonic, not deployable |

The val-to-eval AUC drop confirms regime drift between 2023 and 2024. The model learned
something real, because the heuristic policy is effectively random, but the signal degrades
out of distribution. The `SIDEWAYS` segment is excluded from deployment because the eval
sample is too small.

### Top Feature Importances

The `TRENDING` model ranked these as most predictive of entry quality:

| Rank | Feature | Category | Interpretation |
| --- | --- | --- | --- |
| 1 | `atm_pe_close` | Option state | Raw put premium level, proxy for IV and demand |
| 2 | `atm_ce_close` | Option state | Raw call premium level, proxy for IV and demand |
| 3 | `minutes_since_open` | Session timing | Time of day, later entries have less time value |
| 4 | `atm_ce_oi_change_30m` | OI flow | Call-side institutional positioning shift |
| 5 | `atm_pe_vol_ratio` | Options activity | Put-side relative volume |
| 6 | `atm_pe_oi_change_30m` | OI flow | Put-side institutional positioning shift |
| 7 | `price_vs_orl` | Structure | Distance from opening-range low |
| 8 | `iv_percentile` | Premium cost | Absolute expensiveness |
| 9 | `iv_skew` | Options skew | Put/call imbalance |
| 10 | `fut_return_15m` | Momentum | Underlying momentum over 15 minutes |

Feature quality assessment:

The model is learning option market microstructure. Premium level, OI flow, and session
timing predict follow-through better than underlying price momentum alone. This matches
experienced options-trader intuition.

## 3. Asymmetric Threshold Design

A fixed threshold of `0.60` applied uniformly was too blunt. It blocked high-quality
`OI_BUILDUP` setups at the same rate as structurally weak `EMA_CROSSOVER` setups.
Strategy-aware thresholds were introduced after repeated comparison runs showed that the
strategies respond differently to the same model score.

| Strategy | Threshold | Rationale |
| --- | ---: | --- |
| `OI_BUILDUP` | 0.50 | Positive contributor; preserve coverage while filtering weak setups |
| `ORB` | 0.65 | Unstable by year and regime; require stronger confidence |
| `EMA_CROSSOVER` | 0.80 | Structurally weak in multiple periods; only allow top-scored setups |
| `VWAP_RECLAIM` | 0.60 | Insufficient sample; keep neutral until more data |
| `PREV_DAY_LEVEL` | 0.65 | Insufficient sample; use moderate bar |

## 4. Full-History Comparison (2020-2024)

In-sample caveat:

The segmented ML bundle was trained and calibrated on data spanning this same overall period.
The full-history replay is therefore partly in-sample and should be treated as directional
research, not as out-of-sample proof. The 2024 holdout below is the clean evaluation.

Dataset:

- `2020-01-01` to `2024-02-09`

| Mode | Trades | Win Rate | Profit Factor | Capital Return | Max Drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| Deterministic baseline | 413 | 45.3% | 1.01 | -0.47% | -1.47% |
| ML asymmetric thresholds | 254 | 55.9% | 1.64 | +3.21% | -0.69% |

### Strategy Contribution Breakdown

| Strategy | Deterministic | ML | Delta | Interpretation |
| --- | ---: | ---: | ---: | --- |
| `OI_BUILDUP` | -0.23% | +1.08% | +1.31% | Model likely promoting stronger OI setups |
| `EMA_CROSSOVER` | -0.40% | +1.28% | +1.68% | Suspicious; likely partly in-sample |
| `ORB` | +0.27% | +0.75% | +0.48% | Consistent with filtering weak ORB setups |

`EMA_CROSSOVER` swinging from `-0.40%` to `+1.28%` is the most suspicious number in the
full-history result. EMA has been structurally negative across multiple configurations.
A `+1.68%` improvement from threshold filtering alone, without more data, is more likely to
reflect in-sample selection than a genuine discovery.

## 5. Holdout Comparison (2024-01-01 to 2024-02-09)

Evaluation status:

This is the clean evaluation because the ML bundle was not trained on 2024 data.
The numbers are interpretable as genuine out-of-sample behavior. However, there were only
18 deterministic trades and 5 ML trades, so single-trade effects dominate. Treat the
patterns as directional signals, not statistically stable conclusions.

| Mode | Trades | Win Rate | Profit Factor | Capital Return | Max Drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| Deterministic baseline | 18 | 50.0% | 0.602 | -0.235% | -0.445% |
| ML asymmetric thresholds | 5 | 40.0% | 0.956 | -0.015% | -0.105% |

### Strategy-Level Holdout Breakdown

| Strategy | Deterministic | ML | Signal Quality |
| --- | --- | --- | --- |
| `OI_BUILDUP` | 1 trade / +0.065% | 2 trades / -0.032% | Not interpretable |
| `EMA_CROSSOVER` | 12 trades / -0.292% | 2 trades / +0.048% | Still too small to trust |
| `ORB` | 5 trades / -0.008% | 1 trade / -0.030% | Not interpretable |

What the holdout confirms:

- drawdown improvement is robust: `-0.445%` to `-0.105%`
- trade reduction was aggressive: `18` to `5`
- capital return moved from clearly negative to almost flat: `-0.235%` to `-0.015%`
- `EMA_CROSSOVER` did not revert negative, but two trades is not evidence of a real fix
- `OI_BUILDUP` flipped sign, but 1 vs 2 trades is not interpretable as a stable negative signal

## 6. Consolidated Findings

### What Is Established

| Finding | Evidence Quality | Source |
| --- | --- | --- |
| ML model learns real signal better than heuristic | Strong | Holdout ROC AUC beats heuristic materially |
| Asymmetric thresholds are the right architecture | Strong | Multiple comparison runs |
| ML acts as a risk filter and improves drawdown | Strong | Held across full-history and holdout |
| `OI_BUILDUP` is the core positive contributor | Strong | Repeated across deterministic and ML research |
| `EMA_CROSSOVER` is structurally weak | Strong | Negative in multiple periods and configurations |
| ML does not yet prove positive holdout capital return | Moderate | Holdout is much less negative, not decisively positive |
| `EMA` fixed by 0.80 threshold | Weak | Two holdout trades only |
| `ORB` characterization | Weak | Too unstable across windows |

### What Is Not Established

- whether asymmetric thresholds improve capital return on a large clean holdout
- whether `EMA_CROSSOVER` is genuinely improved by the `0.80` threshold
- whether `OI_BUILDUP x TRENDING` is stable enough to be the primary portfolio driver at scale
- whether SHAP feature importances are stable across years
- whether ML helps or hurts exit timing; no exit-model work has been done yet

## 7. Recommendation

Decision:

- do not deploy `MLEntryPolicy` to production yet
- do not run further threshold experiments on the current dataset
- move to Phase 1: data expansion

Why:

The 2024 holdout has only 18 deterministic trades. The cleanest strategy-level cell has
1 to 2 trades per configuration. Additional tuning runs on this slice will generate
configuration-specific noise, not interpretable insight.

The full-history result is large enough to show direction and it does, but it is partly
in-sample for the ML model and cannot be the deployment gate.

The ML scaffolding is complete and working. The model is learning real structure.
The asymmetric-threshold architecture is correct. What is missing is the data volume
required to separate signal from noise at the strategy level.

### Phase 1 Tasks

- audit raw archive date range, gaps, and field coverage
- rebuild snapshot parquet over the full archive
- verify required `SnapshotAccessor` fields are populated:
  - `ema_9`, `ema_21`, `ema_50`
  - `vwap`, `price_vs_vwap`
  - `or_width`
  - `pcr_change_30m`
  - ATM option OI and volume ratio fields
- rerun deterministic baseline on expanded history
- target 500+ clean trading days and 1,500+ candidate entry votes

### After Phase 1

- retrain segmented models on the larger dataset
- rerun the asymmetric-threshold comparison on a large clean holdout
- verify `OI_BUILDUP` contribution remains stable
- verify `EMA` behavior at `0.80` threshold on a materially larger sample
- compute SHAP on train, valid, and eval separately and compare feature stability

Deployment gate:

ML asymmetric thresholds should improve capital return and profit factor on a large clean
holdout, while `OI_BUILDUP` remains the dominant positive contributor.

---

ML Entry Policy Research Memo · March 2026 · Internal document · Do not distribute
