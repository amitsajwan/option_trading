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

## Amendment 1 (pre-registered 2026-07-11, before running): Exp-2 & Exp-3
G4-as-specified FAILED (+₹33,604 vs +₹125,398): the frozen train-p90 threshold
over-blocked in the hotter 2026 regime (scores are non-stationary — thresholds
must be ADAPTIVE). The tail-kill evidence (worst −₹2.7k vs −₹15.7k) justifies
exactly two follow-ups, one shot each, judged as committed:
- **Exp-2 crash-only entry veto**: skip entry when score ≥ rolling p97 of the
  trailing ~30 sessions' scores (adaptive), warmup-inactive.
- **Exp-3 movement exit-tripwire**: while holding, score ≥ rolling p97 for 2
  consecutive bars → exit the spread immediately (reason movement_tripwire).
PASS bar for each: 19-mo total ≥ +₹125,398 AND sum of 5 worst losses improved
vs baseline. No further threshold iterations beyond these two runs.

## Amendment 2 (pre-registered 2026-07-11 PM): NIFTY iteration
NIFTY v1 FAILED G1 (0.7498). Design hypothesis (not threshold-fishing): the
label should match EVENT RARITY (~4-6% base), not BN's raw 0.20%; NIFTY's
lower vol makes 0.20%/5m too rare (2-3%) and noisy to learn. Procedure:
1. NIFTY label study (same candidates + 0.15%/5m), pick by walk-forward AUC.
2. Train NIFTY v2 on the winner; SAME gates G1-G3 (0.78/3x/0.05) — unchanged.
3. Benchmark: BN model scored cross-instrument on NIFTY (no training).
4. If gates pass: NIFTY replay with adaptive veto must beat +₹23,377 → shadow.
One iteration. If v2 fails G1, NIFTY movement modeling is parked (not retried
weekly) until a materially new feature or data source exists.

## Amendment 3 (pre-registered 2026-07-11 eve): straddle-on-fire study (BN)
First buyer P&L test of the movement model — direction-free by construction.
Design: on each fire-line crossing (score >= 0.30, no position open, one
entry per crossing), BUY the ATM straddle at the bar's recorded CE+PE prices;
exit BOTH legs at the SAME strikes' recorded prices t+5m later (also report
10m/15m variants); costs = 4 pts slippage (1pt/leg/side, seller-replay
convention) + 3 pts statutory per round trip = 7 pts, applied per trade.
PASS bar: post-cost total > 0 in BOTH halves of the 19-month window AND in
the 2026-04+ holdout months. Fail => buyer stays signal-only; no variants
beyond the three reported horizons.
RESULT 2026-07-11: FAIL at every horizon (direction-free straddle bleeds).

## Amendment 4 (pre-registered 2026-07-11 night, before running): fire x lever x debit vertical (BN)
Hypothesis: Amendment 3 failed because a straddle pays for BOTH sides; the
validated ~61% big-move direction lever (project_direction_lever_2026-06-10)
should keep only the paid-for side, and a debit vertical caps the premium
bleed that killed naked buys.
Design, one shot, judged as committed:
- Same fire definition as A3: score >= 0.30 crossing (prev bar < 0.30), no
  position open, one entry per crossing, BN 19-mo quality-gated snapshots.
- Direction: the LIVE serving lever `_detect_agreement_lever` (mom15 +
  max_pain + OI, all-agree-else-abstain). ABSTAIN => no trade (logged).
- Structure: debit vertical in lever direction at recorded chain prices:
  BUY ATM, SELL ATM+100 for CE (ATM-100 for PE). Exit both legs at the same
  strikes' recorded prices t+5m (report 10m/15m variants).
- Costs: 7 pts per round trip (4 pts slippage + 3 statutory — A3 convention).
- PASS bar (same shape as A3): post-cost total > 0 in BOTH halves AND in the
  2026-04+ holdout. Also reported: abstain rate on fires, per-side hit rate.
Fail => buyer stays signal-only; no structure/threshold variants beyond the
three horizons. Pass => paper-tier live wiring proposal (not auto-live).
RESULT 2026-07-11 night: INCONCLUSIVE — NOT a verdict. 331 fires, 100%
abstain: backfilled snapshots carry per-strike OI but never computed the
chain aggregates the lever reads (max_pain, atm_oi = None), so the trio can
never reach 3-agree. Two run notes: (1) first run scored the raw serving row
and got 0 fires in 412 days — the EMA train/serve skew (see
project_ema_serving_gap memory); fixed via per-day canonical enrichment.
(2) The same skew means the LIVE fire-line is mute until serving gets
stateful EMA. Permitted follow-up (data repair, not iteration): enrich
backfill with max_pain/atm_oi derived from the stored chain, rerun A4 ONCE
as registered. The lever's 61% claim itself is NOT in question here.
RERUN RESULT 2026-07-11 late night (after data repair): **FAIL, decisive.**
316 fires, 91% abstain, 30 trades. Post-cost NEGATIVE at every horizon
(5m -208pts/Rs-6,252 win 10%; 10m -173pts; 15m -160pts win 20%), negative
in BOTH halves and the 2026-04+ holdout, CE and PE legs both negative.
The lever hit direction 17/30 = 57% (consistent with its ~61% claim) — yet
the vertical still lost: at fire moments the chain is already repriced for
the move, so even directionally-right debit structures bleed. Foresight is
priced in offensively; it only pays defensively (the veto). Buyer stays
signal-only. The buyer file is now closed with direction-free (A3) AND
direction-aware (A4) evidence.
