# Model Card V2 Addendum (T24)

This addendum extends `model_card_v1.md` with Phase-2 exit-aware behavior.

## 1. Version Delta

- Base model remains CE/PE probability estimator from V1 (`t06_baseline_model.joblib`).
- Phase-2 adds execution semantics and evaluation layers:
  - path-aware label outcomes (T15)
  - intrabar exit simulation (T16)
  - dynamic exit policy and optimization (T17-T18)
  - profile comparison and fill/slippage stress (T19-T20)
  - replay and execution drift monitoring (T21-T23)

## 2. Intended Use (V2)

- Use model probabilities for entry timing.
- Use selected Phase-2 profile for exit behavior.
- Primary mode remains paper/replay validation before production migration.

## 3. Active Operating Profile

From `ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json`:

- `best_profile.name`: `path_v2_best_t18`
- `execution_mode`: `path_v2`
- `intrabar_tie_break`: `tp`
- `slippage_per_trade`: `0.0`
- `forced_eod_exit_time`: `15:24`
- `cost_per_trade`: `0.0006`

## 4. Observed Performance (Current Artifacts)

T19 best summary (profile-comparison baseline):

- `trades_total`: `255`
- `net_return_sum`: `1.9683`
- `mean_net_return_per_trade`: `0.00772`
- `win_rate`: `0.5529`
- `max_drawdown`: `-0.6275`
- `exit_reason_counts`: `time=248`, `forced_eod=5`, `tp=2`

T20 stress test (liquidity-adjusted fill model):

- `mean_slippage_total`: `0.01016`
- `net_return_sum`: `-0.6219`
- `max_drawdown`: `-1.2941`

Interpretation: profitability is sensitive to execution-cost realism; profile decisions must be reviewed with fill assumptions.

T21 replay evaluation (paper decision mapping):

- `buy_decisions_total`: `147`
- `matched_trades`: `147`
- `match_rate`: `1.0`
- `net_return_sum`: `3.5129`

## 5. Risks and Limitations (V2)

- Exit-quality gains can be negated under realistic slippage regimes.
- Current folds are limited in count; out-of-sample stability is still fragile.
- Replay alignment is strict timestamp-based and may degrade if upstream timestamp normalization changes.
- Intrabar tie-break is deterministic but still a modeling assumption.

## 6. Monitoring Requirements

- Track `event_type_distribution.max_shift`, `exit_reason_distribution.max_shift`, and hold-duration shifts via T23.
- Do not promote profile changes without:
  - updated T19 ranking
  - T20 cost-stress review
  - T21 replay parity review

## 7. Phase 3 Addendum (T32-T34)

Current Phase 3 evidence (latest artifacts):

- T32 diagnostics (`t32_diagnostics_stress_report.json`):
  - rows: `186,628` across `498` days
  - CE brier gap (valid-train): `+0.00126`
  - PE brier gap (valid-train): `+0.00043`
  - baseline dual stress point (cost=0.0006, slippage=0.0):
    - trades: `16`
    - net_return_sum: `-0.1632`
    - win_rate: `0.4375`
- T33 runtime guards (`t33_order_runtime_report.json`):
  - intents deduped: `103`
  - matched intents: `103/103`
  - side/kind mismatch: `0/0`
  - guard status: `halt` (`kill_switch=true`) due:
    - max consecutive losses `20` (threshold `4`)
    - max drawdown `-1.0` (threshold `-0.3`)
- T34 reproducibility (`t34_phase3_reproducibility_report.json`):
  - status: `pass`
  - compared artifacts: `4/4`
  - mismatches: `0`

Operational interpretation:

- Reconciliation integrity is strong (idempotent + matched).
- Current runtime guard state is a strict `NO-GO` for live capital.
- Reproducibility for the Phase 3 evaluation stack is deterministic.
