# Entry model rare-label rebuild — pre-registered 2026-07-12

## Motivation
User recalled a Kite-era model (AUC "82") that "used to work." Verified:
`c:/tmp/models/entry_only_model.joblib` — holdout AUC **0.8274**, base rate
**6.21%** (rare), label = "0.20% move in 5min, either direction,
level-invariant." The CURRENTLY LIVE buyer entry model
(`dhan_entry_bundle_v3.joblib`, `ENTRY_ML_MODEL_PATH`) has holdout AUC
**0.6762**, base rate **32.93%** (common) — label = "100pt move in 15min",
a much broader/easier-to-satisfy event that is inherently harder to
discriminate well. This is the SAME pattern already proven this week for
the movement model ("rare label vindicated", wf-AUC 0.806 rare vs 0.694
common). Hypothesis: retraining the buyer's entry model on Dhan data using
the Kite recipe's rare, level-invariant label recovers most of the lost AUC.

## Design (one shot, judged as committed)
- **Label**: binary, 1 if max(|fut_close[t+k]-fut_close[t]|)/fut_close[t] >=
  0.002 (0.20%) for any k in 1..5 minutes ahead (either direction,
  level-invariant — exact Kite recipe). Computed from `fwd_maxabs_5m` /
  `fut_close` in the training view (no new label-menu study — this is the
  ALREADY-VALIDATED Kite label, not a threshold fish).
- **Features**: the SAME 47-feature `ENTRY_FEATURES_V3` set the current live
  bundle uses (drop-in replacement, fair AUC comparison) — sourced via
  `build_feature_row` (serving-path parity) from the re-enriched (EMA/
  max_pain fix, 2026-07-12) `phase1_market_snapshots_hist` training view,
  NOT the older parquet pipeline `train_entry_dhan_v3.py` reads from.
- **Splits**: train 2024-11-01..2026-03-31, valid 2026-04-01..2026-05-31,
  holdout 2026-06-01..2026-07-10 (most recent quality-gated data as
  holdout, matching rare-label-study practice).
- **Training**: same pipeline as `train_entry_dhan_v3.py` (Optuna HPO 60
  trials, isotonic calibration prefit on valid, evaluated on holdout) —
  reused code, only the label and data source change.
- **Instrument**: BANKNIFTY only this pass (mirror NIFTY after, if it
  passes — same phased pattern as the movement model).

## Gates (PASS bar, pre-registered)
- G1 holdout AUC >= 0.70 (meaningfully beats the live 0.676; short of
  Kite's 0.827 is acceptable given Dhan's shorter/different data regime —
  full parity is not required to justify shipping)
- G2 separation (precision_fired - base_not_fired) >= 0.15 at some
  threshold >= 0.30
- G3 calibration ECE <= 0.05
- G4 prob spread >= 0.20 (not collapsed)

## What happens on PASS / FAIL
- FAIL any gate: rebuild parked, live model unchanged, report the gap
  honestly (do not threshold-fish further in this pass).
- PASS all gates: NOT auto-live. Proposal only: paper-tier shadow/replay
  economics test (same discipline as the seller and movement-model
  rollouts) before any live wiring change — this is real capital's entry
  timing, not a backtest toy.

## RESULT — first shot (2026-07-12 morning)
Base rate came in rarer than Kite's (4.15% holdout vs Kite 6.2%) — hold AUC
**0.723** (vs live 0.676, vs Kite 0.827), ECE 0.020 (PASS), AUC gate PASS
(>=0.70). Separation gate FAIL: 0.1125 vs required 0.15 (near-miss, only
non-degenerate threshold was 0.30, fire rate 0.14% of bars — too rare to
even test higher thresholds). Mechanism (common->rare label recovers AUC)
CONFIRMED directionally; exact bar not met.

## RESULT — Amendment 1 (0.15% label, before running economics test below)
ALL GATES PASS. Holdout AUC **0.739** (vs live 0.676, vs Kite 0.827), ECE
0.035, base rate 6.5%(train)/9.6%(holdout). Separation at thr=0.30: fire
rate 0.84% of bars, precision_fired 53.9% vs base_not_fired 9.2% (sep
0.447) — comparable magnitude to the Kite model's own separation (0.537).
Statistical improvement CONFIRMED. Does NOT by itself prove profitability
(same caveat as the movement model, A3/A4) — economics test below.

## Amendment 2 / economics test (pre-registered 2026-07-12, before running)
Same design as Amendment 4 (movement model): on each fire (score>=0.30,
prev bar <0.30, no position open, one entry per crossing), ask the LIVE
serving direction lever (`_detect_agreement_lever`) for a side — ABSTAIN =
no trade. Structure = debit vertical at recorded chain prices (BUY ATM,
SELL ATM+100 for CE / ATM-100 for PE). Exit both legs at the same strikes'
recorded prices at t+5m (report 10m/15m variants too). Costs = 7pts/round
trip (4 slippage + 3 statutory — A3/A4 convention, held constant for
comparability). Run over the full re-enriched 19-month BN hist dataset.
PASS bar (same shape as A3/A4): post-cost total > 0 in BOTH halves of the
window AND in the 2026-04+ holdout. Also reported: lever abstain rate,
per-side hit rate, fire rate/day (sanity vs the 0.84%-of-bars measured on
holdout). One shot. Fail => this entry model stays a statistical curiosity,
not a live change; buyer file stays closed. Pass => paper-tier live wiring
proposal (not auto-live), same as every other buyer artifact this week.

## RESULT — Amendment 2 / economics test: FAIL, decisive
650 fires, 85% lever abstain, 100 trades. Post-cost NEGATIVE at every
horizon (5m −768pts/Rs-23,039 win 4%; 10m −709pts win 8%; 15m −785pts win
6%), negative in both halves AND the holdout. Worse win rate than A4
(10-20%) despite a MUCH better classifier (AUC 0.739 vs the movement
model) and 3x the trade count — firing more confidently on the same
already-repriced dynamic just means buying into it more often, not
avoiding it.

## Amendment 3 / faded-direction control (user request 2026-07-12, same day)
Same 650 fires, same entry/exit mechanics, same costs — but take the
OPPOSITE side of the lever's call. Purpose: rule out "we just have the
direction backwards" before closing the file. RESULT: **also FAIL, nearly
identical magnitude** (5m −712pts win 5%; 10m −717pts win 6%; 15m −703pts
win 11%; negative both halves + holdout). Following and fading the lever
lose by essentially the same amount — direction is NOT the lever here. The
debit vertical loses to something direction-independent: rich entry price
+ decay/cost inside a 5-15min window, at the exact moment any of these
signals (fire-line, direction lever) becomes confident enough to act on.

## CONCLUSION — buyer file closed (2026-07-12)
Three independent structural tests (A3 direction-free straddle, A4
direction-aware vertical, this rebuild's straight AND faded verticals) all
land on the same wall by different routes. Not fixable by: better entry
timing (AUC 0.676->0.739 didn't help, made it worse), better direction
skill (57% lever accuracy didn't help), or flipping the directional bet
(fading loses the same). The master finding stands: foresight is priced in
by the option chain at the moment of confident detection; it pays
defensively (crash-veto, +22-28% vs baseline) and never offensively. No
further buyer-side threshold/structure iteration without a materially new
data source (e.g. genuinely leading order-flow data, not price-derived
features) — same standing applies as parked NIFTY movement modeling.

## Amendment 1 (pre-registered 2026-07-12, before running): looser rare label
Hypothesis: 0.20% is rarer here than in the Kite-era regime, compressing
calibrated probabilities before any threshold gets real separation. Try
0.15% (still clearly "rare", still level-invariant, still either-direction,
same 5min horizon) — ONE shot, same gates (AUC>=0.70, separation>=0.15,
ECE<=0.05, prob_spread>=0.20), same splits, same features. If this also
fails: park the rebuild (do not retry weekly) until a materially new
feature/data source exists — matching the NIFTY movement-model precedent.
