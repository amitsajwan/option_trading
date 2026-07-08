# DORMANT — built but not enabled

Rule (2026-07-07): **a feature is not "done" when built — it is done when it has
run enabled in live and been observed working once.** Three incidents in a row
were already-solved problems lying disabled (giveback stop, config
consolidation, VIX fallback never exercised). Every default-off flag and every
built-not-deployed system lives here with a decide-by date. Review when adding
a new flag; delete rows when enabled-and-observed or when the code is removed.

| What | Where | Off since | Decide by | Decision |
|---|---|---|---|---|
| ~~Giveback stop~~ | `EXIT_GIVEBACK_STOP_ENABLED` | 2026-06-04 | — | **ENABLED 2026-07-07** (replay-verified; watch first live giveback exit) |
| Config consolidation (yml+registry+loader) | `ops/strategy_config.yml`, built 2026-06-14, tests green, VM cutover never done | 2026-06-14 | 2026-07-21 | Deploy or delete. Contract checker (2026-07-07) covers the worst gap meanwhile |
| Opportunity gate (score→rank→select) | `OPPORTUNITY_GATE_ENABLED`, engine :962, 9 tests, P&L unproven | 2026-06-14 | 2026-07-21 | Needs an A/B replay before enabling; else delete |
| Entry pipeline v2 (gate cascade) | `STRATEGY_ENTRY_PIPELINE_V2`, engine :213, deployed flag-OFF 2026-06-03 | 2026-06-03 | 2026-07-21 | Superseded by ML_ENTRY path? Likely delete |
| Intelligent brain shadow | `INTELLIGENT_BRAIN_SHADOW`, engine :225, read-only scorer | 2026-06-06 | 2026-07-28 | Shadow-only by design; decide if the comparison data is ever read |
| Vol-gate entry (non-ML trigger A/B) | `ENTRY_VOL_GATE_ENABLED`, strategy_router :194 | 2026-06 | 2026-07-28 | Keep as A/B lever or delete |
| Live-only entry gate (dual book) | `ENTRY_LIVE_ONLY_GATE`, engine :157 | 2026-06-03 | 2026-07-28 | Tied to paper/live tiering decision |
| Expiry exit override stack | `EXIT_EXPIRY_OVERRIDE_ENABLED`, exit_policy :499 | 2026-06 | 2026-07-21 | June 30 replay suggests expiry days ARE the edge — evaluate enabling |
| Gemini session bias / grounding | `GEMINI_*`, brain/session_bias.py | 2026-06-11 | 2026-08-04 | Flaky when live; decide keep/delete |
| LLM oversight | shadow-only, gated on profitability | 2026-06-08 | 2026-08-04 | A/B showed no improvement — candidate for deletion |
| ~~S3 seller system (iron condor)~~ | seller_app | 2026-06-12 | — | **ENABLED 2026-07-08, real money** (explicit user decision, skipped the design doc's own "paper first" gate). 8/8 live-paper trades +₹33,204 reviewed before go-live; code safety audited; cross-system BN-conflict guard added. See project memory for full detail |

## Closed investigations (do not re-attempt without new evidence)

| What | Tried | Verdict |
|---|---|---|
| NIFTY direction ML | 2026-07-08: 3 windows tested — 5yr (AUC 0.567), 2yr/2024-start (0.525), BN-matching recency (0.4727, below coinflip). Monotonically WORSE as window narrows toward BN's recipe — not a recency problem. | **Not learnable at this horizon with this feature set.** Composite (rule-based) is the permanent answer for NIFTY, not a placeholder. Don't retry without new features or a different label. |
| CHOP regime entry (`ENTRY_ALLOW_CHOP=1`) | 2026-07-08: BN June+July replay WITH tonight's improved exit stack (giveback + stale-timestop). 33→44 trades, but win rate 48%→43%, PnL +79.86%→+74.24% (the +11 CHOP trades net −5.62% as a group; Jul-7 alone turned a correct zero-trade day into −10.2%). NIFTY: 0 trades either way (model rarely clears threshold in CHOP). | **Confirms the 2026-06-03 finding — CHOP entries are a net negative even with the current exit stack.** Flag stays OFF. Don't re-test without a fundamentally different trigger for CHOP specifically (not just a lower bar on the same model). |

## Watching (enabled recently, not yet observed working in live)

| What | Enabled | Observe |
|---|---|---|
| Giveback stop (both instruments) | 2026-07-07 | First `GIVEBACK_STOP`/trailing dead-zone exit in live traces |
| VIX via fixed REST fallback | 2026-07-07 | `vix_current` non-null in snapshots from the open |
| BN `ENTRY_ML_MIN_PROB=0.25` | 2026-07-08 | Replay-verified (June+July, thr swept 0.20/0.25/0.35): 0.25 is the local optimum — 37 trades, 51% win (vs 33/48%/+79.86% at 0.35), +84.60% PnL. 0.20 tested worse (39 trades, 48% win, +74.02%). NIFTY left at 0.35 — no positive evidence at lower thresholds (sample too thin, June rebuild data unavailable tonight) |
| S3 seller BN-conflict guard | 2026-07-08 | First live cycle where BOTH BN buy-side and seller are active simultaneously — confirm the guard actually skips an entry when it should (check `entry_skipped_bn_conflict` in seller logs) |
| NIFTY composite direction | 2026-07-06 | First NIFTY live entry with `direction_source=composite` |
| Token guard timer (15-min probe) | 2026-07-07 | A day of `guard: token healthy` journal lines |
| Config contract + feature health + deploy.sh | 2026-07-07 | First green run of each on the VM |
| NIFTY fast entry model-2 (thr 0.08) | 2026-07-08 | First live NIFTY entry with `deciding_model: m2` |
| BN fast entry model-2 (thr 0.6) | 2026-07-08 | First live BN entry with `deciding_model: m2` |
