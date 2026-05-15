# ML Model State — 2026-05-15

> **Read this first when resuming.** Today produced two pieces of overfit evidence and one running experiment (B1, completes Saturday early morning IST).

---

## TL;DR

- C1's apparent 2024 edge is **period-specific**. Holdout (16 trades, 2024-08-01 → 2024-09-24) is **net −55% at 200 bps**, PF 0.86.
- Exit-timing sweep on C1's 107 entries with hold ∈ {9, 15, 20, 30} bars: **all four net-negative on the holdout window**. Exit timing is not the lever.
- **F1 walk-forward** (C1 recipe, training window shifted 12 months back so 2024 is unseen): trained successfully in ~2 hrs but **HELD** at `stage1_cv_gate_failed` — `block_rate=1.0` on every fold. The model would refuse every entry. Confirms the recipe is highly window-sensitive.
- **B1 cost-aware label** (C1 recipe + windows, but `cost_per_trade = 0.02` instead of 6 bps): launched 2026-05-15 17:46 UTC, ETA ~6 hrs. Tests whether labels generated against realistic option friction produce a more selective and OOS-stable model.
- **Operator directive:** do NOT deploy real capital. Wait for B1 result + at least 2 weeks of forward shadow data.

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

### 2.4 B1 cost-aware label — running

B1 hypothesis: maybe the recipe DOES generalize, but only when labels reflect realistic option cost. C1's labels are generated with `cost_per_trade = 0.0006` (6 bps). At that cost almost every directional move is labeled "profitable side"; the model learns to be permissive. In production we pay 200 bps. The label-train mismatch could explain why the recipe doesn't translate.

B1 config: identical to C1 (same window, same features, same HPO) but `cost_per_trade = 0.02` (200 bps).

| Field | Value |
|---|---|
| config | `ml_pipeline_2/configs/research/staged_dual_recipe.deep_hpo_b1_optcost_200bps.json` |
| outputs.run_name | `deep_hpo_b1_optcost_200bps` |
| entity_id | `deep_hpo_b1_optcost_200bps_20260515_174558` |
| manifest_hash | `79ce021ec4da0d18964a853066a69de076b3f773773adfdfb410cfd031e290ac` |
| Launched | 2026-05-15 17:46 UTC |
| Tmux session | `pathb1` on `option-trading-ml-01` |
| ETA | ~6 hrs (Saturday ~00:00 IST = Friday ~18:30 UTC) |

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

## 6. Decision matrix — Saturday morning IST

When B1 completes, look at its `summary.json` for the same fields F1 had (`status`, `completion_mode`, `roc_auc`, `block_rate`, `blocking_reasons`).

| B1 outcome | Combined picture | Next action |
|---|---|---|
| ✅ Publishes (passes all gates) | Cost-aware label was the missing piece — recipe works under realistic cost | Replace C1 with B1; start forward shadow Monday to validate live OOS |
| ❌ Held (any gate) | Recipe is over-fit to BOTH window (F1) and cost (B1) | Fundamental rethink: try direct-option-P&L label, or different feature set, or shelve. NO more HPO tuning on the same recipe. |
| 🟡 Marginal (publishes with thin gates) | Cost helps but isn't sufficient | Use B1 as starting point for the next experiment (e.g., B2 with `cost_per_trade=0.01`) |

## 7. What NOT to do next session

- Don't iterate on exit timing, strike selection, or frequency thresholds. Today's data closes those questions.
- Don't run more HPO trials on C1's recipe with different windows or different costs — F1 and B1 cover both axes.
- Don't deploy real capital regardless of B1's outcome. Forward shadow is the prerequisite.
- Don't celebrate in-sample numbers. The UI date-picker now visually marks training vs holdout dates — use it.

## 8. What ARE the open experiments

- B1 (running) — see §2.4 above. Auto-completes Saturday.
- Forward shadow collection — blocked on Kite live credentials + Monday market open.
- Direct-option-P&L label experiment — only worth designing if B1 fails. New label: "did this option trade make profit after 200 bps?" predicted directly, not via futures direction. Would be a new training run, not just a config tweak.

## 9. Pipeline file references

For anyone reading the code:

- Cost-in-label application: [`staged/pipeline.py` lines 295-296](../../src/ml_pipeline_2/staged/pipeline.py#L295) — `ce_net = path_return − cost_per_trade`
- Stage 1 CV gate eval: [`staged/pipeline.py`](../../src/ml_pipeline_2/staged/pipeline.py) — search for `stage1_cv_gate` or `block_rate`
- Direction label: `direction_market_up_v1` — labeler picks side with higher `best_*_net_return_after_cost`
