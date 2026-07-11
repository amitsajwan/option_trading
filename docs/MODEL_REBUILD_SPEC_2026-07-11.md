# Movement Model Rebuild — Pre-registered Spec (2026-07-11)

Signed off by user 2026-07-11 ("Going good go for it") after the label study.
This spec is committed BEFORE training. Results are judged against it as-is;
no post-hoc renegotiation. (Protection against the 0.35-threshold class of
self-deception.)

## Evidence base (label study, 19mo quality-gated data, 131k bars)
Walk-forward AUC by label: pct0.20/5m **0.806** (0.772-0.835, consistent);
pct0.30/15m 0.766; abs100pt/15m (current model) 0.694. The 2026-07 Dhan
retrain's label switch from rare(6%) to common(22%) events cost ~0.11 AUC —
the models were never broken; the label was.

## Decisions
- **Label**: `fwd_maxabs_5m >= 0.20% of fut_close` (the validated rare label).
- **Features**: the serving feature set (build_feature_row union, incl. the
  EMA family restored 2026-07-11). No new features in v1.
- **Splits**: train 2024-11-01→2025-12-31; validate 2026-01-01→2026-03-31
  (HPO only); holdout 2026-04-01→2026-05-23 (untouched until gate time).
- **Consumption**: (a) SELLER quiet-gate — skip condor entry when
  P(move) ≥ 90th percentile of the model's TRAIN-window score distribution;
  (b) BUYER fire-line — paper-tier signals only above the smallest score with
  holdout precision ≥ 3× holdout base rate. Both points derived from curves,
  never hand-picked.

## Ship gates (ALL must pass; else the model does not deploy)
1. Holdout AUC ≥ 0.78.
2. Holdout precision at the buyer fire-line ≥ 3× holdout base rate, with ≥ 50
   holdout positives above the line.
3. Calibration ECE ≤ 0.05 on holdout (10-bin).
4. Deployment test: BN 19-mo seller replay (real SellerRunner) with the
   quiet-gate ON must total ≥ the +₹125,398 gate-OFF baseline. If it doesn't,
   the model may still ship for the buyer book, but the seller gate stays OFF.
5. Buyer book remains PAPER-TIER regardless of results (research book policy).

## Rollout
Shadow/paper Monday; live consumption decisions only after ≥1 week of live
paper agreement. NIFTY follows the same spec after BN validates.
