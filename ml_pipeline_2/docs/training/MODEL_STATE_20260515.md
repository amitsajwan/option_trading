# ML Model State — 2026-05-15 (final)

> **Read this first when resuming.** Today closed the C1-recipe research arc with four independent overfit/data-ceiling confirmations. **Training is paused.** Next action is forward shadow data collection, gated on operator sharing Kite live credentials.

---

## TL;DR

Four experiments today, all consistent with the same conclusion:

| # | Test | Result |
|---|---|---|
| 1 | C1 holdout decomposition (2024-08 → 2024-09, 16 trades) | Net **−55% @ 200 bps**, PF 0.86 |
| 2 | Exit-timing sweep on C1's 107 entries (9/15/20/30 bars) | All 4 variants net-negative on holdout |
| 3 | **F1 walk-forward** — C1 recipe, training window shifted to pre-2024 | HELD at `stage1_cv_gate_failed`, block_rate=1.0 |
| 4 | **B1 cost-aware label** — C1 recipe, `cost_per_trade=0.02` in label | HELD at `stage2_signal_check_failed`, block_rate=1.0 |

Additionally: **Path C-1** (conviction-filtered labels via `direction_or_no_trade_v1` + `stage2_target_redesign`) was considered but rejected — it was already documented as a dead-end in the April 26 session (0 holdout trades at 6 bps cost; would be worse at 200 bps).

**Conclusion:** C1's apparent edge depends on a specific (training window × optimistic cost × original label design) combination. The data ceiling on 2020-2024 BankNifty is reached.

**Decision:** training is PAUSED. Don't iterate on C1 recipe further. Don't deploy real capital. The single unblocking action is forward live shadow data collection — needs operator to share Kite live credentials, starts Monday 2026-05-18 09:15 IST.

---

## 1. Why this session existed

Started with the assumption that Phase 1.2 + 1.3 (wider exits + smart-strike) had validated the C1 model end-to-end at **+271% net @ 200 bps over 56 trades** in a 2024 replay. Operator asked the right question — was that result OOS or in-sample? — and the answer reversed everything.

## 2. Findings in order

### 2.1 C1 holdout is net-negative

Splitting the 56-trade Phase 1.2 + 1.3 replay by C1's training windows:

| Window | n | avg gross | net @ 200 bps | PF |
|---|---|---|---|---|
| train (model saw) | 24 | +13.88% | **+285%** | 2.37 |
| valid (light contamination) | 16 | +4.56% | +41% | 1.59 |
| **holdout (CLEAN OOS)** | **16** | **−1.42%** | **−55%** | **0.86** |

Training contributes **+87%** of total gross. The +271% headline was almost entirely "the model recognizes dates it was trained on." See [docs/PROJECT_PLAN.md §14](../../../docs/PROJECT_PLAN.md).

### 2.2 Exit timing doesn't rescue holdout

Counterfactual sweep on C1's full 107 entries with hold ∈ {9, 15, 20, 30} bars, same stop/target/cost. **Holdout-only**:

| Hold bars | n | avg gross | net @ 200 bps | PF |
|---|---|---|---|---|
| 9 (C1 original) | 18 | −2.62% | −83% | 0.65 |
| 15 | 18 | −5.81% | −141% | 0.44 |
| 20 | 18 | −5.88% | −142% | 0.42 |
| 30 (Phase 1.2) | 18 | −3.47% | −99% | 0.65 |

All net-negative. Training window same sweep shows +448% net for 30-bar — the +448% → −99% swing on the same trades is **the textbook signature of severe overfit**. See [scripts/sim_exit_sweep.js](../../../scripts/sim_exit_sweep.js).

### 2.3 F1 walk-forward — held

F1 = C1 recipe, training window shifted 12 months back:
- research_train: 2020-08-03 → **2023-04-30**
- research_valid: 2023-05-01 → 2023-07-31
- final_holdout: 2023-08-01 → 2023-10-31

Launched 2026-05-15 14:42 UTC. Finished in ~2 hrs (faster than C1's ~6 hrs — less training data).

| Field | Value |
|---|---|
| entity_id | `staged_deep_hpo_c1_base_20260515_144206` |
| manifest_hash | `ac8b777c853ffff861b48cdfac77160b50e3cc9118a25c091a6efe7454be3abd` |
| status | `held` |
| completion_mode | `stage1_cv_gate_failed` |
| Stage 1 ROC | 0.642 (C1 was 0.683) |
| block_rate (all folds) | **1.0** |

The Stage 1 entry filter, trained on pre-2024 data, is so conservative that it would refuse 100% of entry candidates on its own validation data. Stage 1 ROC is fine (close to C1); the model has SOME predictive power. But its calibrated threshold can't find positive-EV entries in the older data distribution.

**Interpretation:** the recipe doesn't generalize across time. Combined with C1's holdout failure, this is two independent signals: the recipe over-fits to its specific training window.

### 2.4 B1 cost-aware label — HELD

B1 hypothesis: maybe the recipe generalizes when labels reflect realistic option cost. C1's labels use `cost_per_trade = 0.0006` (6 bps); at that level nearly every directional move is "profitable side." In production we pay 200 bps. The label-cost mismatch could be the missing piece.

B1 config: identical to C1 (same window, same features, same HPO) but `cost_per_trade = 0.02` (200 bps).

**Result: HELD at `stage2_signal_check_failed`.** Finished in only ~10 minutes (Stage 2 pre-check fired early). Same `block_rate=1.0` across all CV folds as F1.

| Field | Value |
|---|---|
| config | `ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_b1_optcost_200bps.json` |
| entity_id | `deep_hpo_b1_optcost_200bps_20260515_174558` |
| manifest_hash | `79ce021ec4da0d18964a853066a69de076b3f773773adfdfb410cfd031e290ac` |
| status | `held` |
| completion_mode | `stage2_signal_check_failed` |
| block_rate (all folds) | **1.0** |
| Duration | ~10 min (terminated at Stage 2 signal check) |

**Interpretation:** when labels demand 200 bps of clearance, the Stage 2 direction signal isn't strong enough to satisfy the pre-publish validation check. The label distribution becomes too sparse / noisy for the recipe to learn a useful direction call. The model isn't broken — its calibrated signal just can't justify firing entries under the more demanding labeling regime.

### 2.5 Path C-1 considered and rejected

The natural next move would be conviction-filtered labels: use `direction_or_no_trade_v1` + enable `stage2_target_redesign` so only high-conviction historical trades are labeled positive. This was **already tried in the April 26 session** (`staged_proper_full_v1_20260426_051531`) — the conviction filter reduced Stage 2 rows from ~57k to ~3k → 0 holdout trades AT 6 BPS COST. Under 200 bps the throughput collapse would be worse. **This is not a viable next experiment.** See [MODEL_STATE_20260426.md](MODEL_STATE_20260426.md) §"What Doesn't Work".

### 2.5 Data acquisition — exhausted free paths

For real OOS validation we need data the model never trained on (2025+). Explored:

| Source | Conclusion |
|---|---|
| Kite Connect Historical API (₹2000/mo) | **Inadequate.** Historical option chain depth across multiple expiries is limited; doesn't give the full 1-min option-chain shape we need. |
| NSE bhavcopy archives | **EOD only.** Free, full coverage 2025+, but daily granularity can't validate our 1-min strategy. (Smoke-tested today: 4568 rows for 5 days of April 2025; works fine for daily-frequency analysis but not our use case.) |
| NSE option-chain live API (`/api/option-chain-indices`) | **Forward only.** Returns current snapshot. Could collect from-now-onwards but no historical. |
| Custom scrapers / Kaggle datasets | **Incomplete or wrong granularity.** User researched; abandoned. |
| Paid feeds (TrueData / GlobalDataFeeds) | ₹1500-3000/month. Hold for now. |

**The only path to 2025+ 1-min option-chain data is forward live collection.** Requires Kite live credentials (not Historical) and Monday 09:15 IST market open.

## 3. Code / infra changes today

- [strategy_app/engines/trade_signal_builder.py](../../../strategy_app/engines/trade_signal_builder.py) — inverted precedence so env overrides (`underlying_stop_pct`, `underlying_target_pct`, `max_hold_bars`) win over recipe defaults. Previously the staged recipe's values silently masked operator config. 5 new unit tests in [test_trade_signal_builder.py](../../../strategy_app/tests/test_trade_signal_builder.py).
- [strategy_app/engines/option_selector.py](../../../strategy_app/engines/option_selector.py) — new module; smart-strike selector (ATM, 1-OTM, reject) gated by `STRATEGY_SMART_STRIKE_ENABLED`. 11 unit tests. **Note:** OTM branch never fired on C1's trade set (confidence below 0.75 threshold); only the IV-reject filter activated.
- [strategy_app/engines/pure_ml_engine.py](../../../strategy_app/engines/pure_ml_engine.py) — wired selector + plumbed `max_hold_bars_override`.
- [docker-compose.yml](../../../docker-compose.yml) — `strategy_persistence_app_historical` `--trace-topic` wiring (was silently hanging without it).
- [market_data_dashboard/static/webapp/terminal-live.jsx](../../../market_data_dashboard/static/webapp/terminal-live.jsx) — date-picker now tags each date `● train / ◐ valid / ○ OOS / post`. Prevents re-celebrating in-sample numbers. Deployed v8.
- Tape P&L scaling fix (×100) across 8 display sites — was showing fractions as percents.

## 4. Scripts added

In [scripts/](../../../scripts/):

- `analyze_jsonl.py` — canonical replay analyzer. Auto window-split, `--run-id`, statistical warning when holdout < 30 trades.
- `sim_exit_sweep.js` — counterfactual exit-timing sweep on a fixed entry set.
- `run_f1_handoff.sh` — polls F1 status from workstation, prints summary on completion.
- `launch_pathb1_when_f1_done.sh` — polls F1, auto-launches B1 when F1 reaches terminal state (`completed` or `held`).

## 5. Commits today

| Hash | Title |
|---|---|
| fc61a89 | feat(strategy): Phase 1.2 + 1.3 wider exits + smart-strike selector |
| 3e5aa70 | docs+scripts: honest OOS verdict on Phase 1.2 + 1.3 — retract +271% headline |
| a81d67c | feat: walk-forward F1 training + UI window-markers + ingestion skeleton |
| c1c77ae | feat(training): Path B1 config — option-aware label (cost_per_trade=0.02) |
| 48a69a4 | fix(scripts): treat F1 'held' status as terminal — launch B1 anyway |

## 6. Decision taken — training PAUSED

With four independent overfit/data-ceiling confirmations (C1 holdout, exit sweep, F1, B1) and Path C-1 known dead-end, **no more training experiments will be run on the 2020-2024 dataset.** The honest reading: the data has been mined for the directional signal the C1 recipe family can extract, and the result doesn't survive structural shifts (window OR cost). More HPO trials = same answer.

The unblocking action is **fresh data**, not more algorithm tweaks.

## 7. What NOT to do next session

- Don't iterate on exit timing, strike selection, or frequency thresholds on C1 recipe.
- Don't run more HPO trials on C1's recipe with different windows or different costs — F1 and B1 cover both axes.
- Don't try conviction-filtered labels (`stage2_target_redesign`) — Apr 26 already proved this dead-ends at 6 bps cost; under 200 bps it'd be worse.
- Don't deploy real capital. Forward shadow data is the prerequisite for any deployment claim.
- Don't celebrate in-sample numbers. The UI date-picker now visually marks training vs holdout dates — use it.

## 8. Open paths

- **Forward shadow collection** — blocked on operator sharing Kite live credentials. Monday 2026-05-18 09:15 IST is the earliest start. Accumulates 1 trading day of fresh OOS per real day. 4-6 weeks builds the dataset needed for the next experiment.
- **Next-experiment design** (do not run until fresh data exists) — three candidates documented in [PROJECT_PLAN.md §15](../../../docs/PROJECT_PLAN.md):
  1. Direct option-P&L label (binary: "did this option trade clear 200 bps?")
  2. Longer prediction horizon (max_hold 60-120 bars at training time)
  3. Different feature philosophy (structural rather than intraday momentum)

## 9. Pipeline file references

For anyone reading the code:

- Cost-in-label application: [`staged/pipeline.py` lines 295-296](../../src/ml_pipeline_2/staged/pipeline.py#L295) — `ce_net = path_return − cost_per_trade`
- Stage 1 CV gate eval: [`staged/pipeline.py`](../../src/ml_pipeline_2/staged/pipeline.py) — search for `stage1_cv_gate` or `block_rate`
- Direction label: `direction_market_up_v1` — labeler picks side with higher `best_*_net_return_after_cost`
