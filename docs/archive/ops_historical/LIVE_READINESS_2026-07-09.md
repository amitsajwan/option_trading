# Live-Readiness Assessment — 2026-07-09

Scope: overnight verify→fix→verify loop over the June replay window, using the
REAL production pipeline (Mongo snapshots → run_sim_publisher → strategy_app_sim,
identical image/code path as live). Supersedes all earlier ad-hoc-script results.

## Verdicts at a glance

| System | Mechanically works? | Edge demonstrated? | Notes |
|---|---|---|---|
| BN buy-side (ML_ENTRY) | YES | **NO — net negative** | −2.05% over 22 June dates at thr 0.35 |
| NIFTY buy-side | YES | **NO — model never fires** | m1 max prob 0.18 vs thr 0.35 over 5 dates |
| S3 seller (BN) | YES (live) | Unverifiable by replay | Daemon is live-only; no replay path exists |
| Exits / risk caps | YES | n/a | Stop, timestop, session cap all fired correctly |
| Ops (token, alerts, deploy) | YES after fixes | n/a | 2 live incidents found+fixed this session |

## 1. BankNifty buy-side — mechanically sound, no June edge

Corrected A/B (22 June dates, real pipeline, RISK_MAX_SESSION_TRADES=6 enforced):

- **thr 0.35: 93 trades, 37W/56L (39.8%), cumulative −2.05%**
- **thr 0.25: 87 trades, 31W/56L (35.6%), cumulative −2.40%**

Actions taken:
- Live `ENTRY_ML_MIN_PROB` reverted 0.25 → **0.35** (deployed 10:43 IST 2026-07-09).
- The earlier "0.25 is better" conclusion was an artifact: the ad-hoc harness
  (`bn_sim_multi.py`) did not enforce the 6-trade session cap (19 trades on a day
  the real pipeline allows 6). **All prior ad-hoc threshold/CHOP conclusions are void.**

Shortcoming (the big one): only 4 of 22 days were net positive. This is consistent
with the standing master verdict (directional option buying has no demonstrated
edge). The system executes correctly; the signal itself does not pay for costs in
a month like June. Live BN buy-side should be treated as running at
proof-of-mechanics stakes, not edge-extraction stakes.

## 2. NIFTY buy-side — main model never fires; fast model fires at noise level

Replay of all 5 available NIFTY dates (Jun30–Jul8, from live-recorded snapshots,
rebuilt sim image, models confirmed loaded AUC 0.8608):

- m1 (`nifty_entry_bundle_v3`, thr 0.35): **0 entries**. Max prob seen 0.177; p90 ≈ 0.03.
- m2 (`nifty_entry_fast_v1`, thr 0.08): 0 entries in replay; max 0.081.

Live confirmation of the same pattern: today's only NIFTY trade fired at
prob **0.081 vs thr 0.08** — the extreme tail of the distribution, i.e. the
threshold sits exactly at noise level — and stopped out −18.25% in 5 bars.

Shortcomings:
- m1 threshold 0.35 is unreachable for this model's live prob distribution.
  Either the model needs recalibration/retraining or the threshold is wrong for it.
  (Holdout AUC 0.8608 vs live probs ≤0.18 suggests train/serve distribution shift.)
- m2 at 0.08 admits trades with essentially no margin above noise.
- Only 5 replay dates exist; NIFTY had no persistence before this session
  (persistence_app_nifty built 2026-07-08 night). Full-June verification needs the
  offline Dhan-historical rebuild + backfill into `phase1_market_snapshots_nifty`.

## 3. S3 seller — cannot be verified by replay at all

`strategy_app.seller` is a wall-clock polling daemon (`run_forever` against live
Mongo/broker state). It has **no date parameter, no snapshot-stream input, no
replay mode**. The 2024 backtest that justified it ran on a separate offline
harness, not this code path.

Shortcoming: the actual live seller code has never been executed against
historical data. Its first real test is live capital. Mitigations in place:
SELLER_MAX_CONCURRENT=1, SELLER_DAILY_LOSS_CAP_RS=6000, BN-conflict guard,
Telegram alerts. Recommended: build a replay shim (inject a clock + market-data
provider into SellerRunner) before scaling size.

## 4. Infrastructure fixes shipped this session (all deployed + verified)

- **Token refresh crash** (UnicodeDecodeError on stray 0x97 byte in .env.compose):
  refresh script now reads/writes with surrogateescape; can't recur.
- **Token guard 400-blind-spot**: guard treated non-401 failures as "inconclusive"
  for 1h+ during live trading. Now 2 consecutive non-200 probes force a refresh.
- **Stale sim image**: strategy_app_sim was built 2026-06-25; every sim silently ran
  old code with missing models (0 trades). Rule reaffirmed: ALWAYS rebuild
  strategy_app_sim before replay batches.
- **Dashboard Multi page**: SIM badge (flat `rollout_stage` key vs nested schema) and
  frozen BN chart (stale legacy Redis OHLC key winning over freshest) — both fixed
  (70075aa), plus /api/trades crash on string `reason` (14388de).
- **persistence_app_nifty**: new service; NIFTY snapshots now persist to Mongo,
  enabling NIFTY replay for the first time (1761 snapshots backfilled).

## 5. Will it work fine in live? — direct answer

Mechanically: **yes**. Entries, exits, stops, session caps, alerts, token
lifecycle, and position tracking all demonstrably function end-to-end (verified
again live this morning: BN entry tracked with per-minute Mongo marks; NIFTY stop
fired correctly).

Economically: **not yet proven, and June evidence is negative for the buy-side.**
The honest pipeline shows BN buy-side lost ~2% over 22 June days and NIFTY's main
model never trades. The seller is the only component with a historically
profitable backtest, and it is exactly the component that cannot be replay-verified.

Priority recommendations:
1. Keep buy-side at minimum size (1 lot) — it is paying tuition, not extracting edge.
2. Recalibrate or retrain NIFTY m1 (live prob dist vs threshold mismatch); pick m2
   threshold from replay percentiles (e.g. p99), not hand-tuning.
3. Build the seller replay shim before any size increase.
4. Backfill full-June NIFTY snapshots via dhan_build_sim_snapshots + backfill script.
