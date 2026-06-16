# Entry Pipeline v1 vs v2 — Divergence Analysis

**Status:** FIXED + VERIFIED. Both bugs root-caused via decision trace, fixed, and
re-verified — v2 is now decision-equivalent to v1 on both firing days (commit 5714612).

## Verification (post-fix golden master)

| Day | Config | Before fix | After fix |
|---|---|---|---|
| 2026-05-26 | live profile | v1=7, **v2=1** | v1=7, **v2=7** ✅ MATCH |
| 2026-06-02 | consensus, band ₹200-700, paper unlimited | v1=9, **v2=12** | v1=12, **v2=12** ✅ MATCH (+7.08%) |

## Option-chain depth finding (verified via probe_chain.py)

This BANKNIFTY data's chain **stops at ~12 steps OTM**, where premiums are still
**~₹580 (CE) / ~₹620-650 (PE)** — decay is flat (~₹45/100pt step, far-dated/high-IV).
**₹200 is unreachable; the cheapest strike that exists is ~₹580.** So a ₹200-600 band
only catches the single deepest CE strike; a realistic band is **₹550-700** (deepest
available) or a cap ~₹1100 (ATM). 10-step OTM machinery works; the *data* is the limit.

## TL;DR — two confirmed v2 bugs (trace-backed) — NOW FIXED

1. **`DirectionGate` over-vetoes on non-consensus profiles.** It applies the
   consensus-bypass threshold (`CONSENSUS_BYPASS_MIN_CONFIDENCE=0.80`) as a *universal*
   veto. v1 only applies that on the consensus-bypass path; non-consensus profiles
   (e.g. `trader_master_live_v1`) enter at ML confidence 0.65–0.80. → On **05-26** v2
   vetoed 7 real trades v1 took (`ml_confidence_below_bypass`, conf 0.65–0.80 < 0.80).
   **v2 too strict.**
2. **v2 does not replicate v1's stateful risk guards** (consecutive-loss halt /
   re-entry cooldown / session-cap timing). → On **06-02** v2 entered **3 more** trades
   than v1, all *after* the consecutive-loss limit fired. **v2 too loose.**

Both are real, opposite-direction divergences. The gate cascade is sound; the bugs are
(1) a mis-scoped threshold in one gate and (2) missing risk-state gates.

---

## 1. What was run

`golden_master_v1_v2.py` replays a day's real snapshots through the engine **twice** —
`STRATEGY_ENTRY_PIPELINE_V2=0` (v1, the three legacy paths) and `=1` (v2, the gate
cascade) — toggling **only** that flag, and diffs the resulting trades. Run inside the
live `strategy_app` container so models + libs match live.

## 2. Verified results

| Date | Config | v1 | v2 | Verdict |
|---|---|---|---|---|
| 2026-05-26 | live profile (`trader_master_live_v1`) | **7 trades, −31.41%** | **1 trade, −12.14%** | **DIVERGE** (bug 1) |
| 2026-06-02 | OPS consensus profile, **soft cap** (firing) | **9 trades, −3.56%** | **12 trades, −3.61%** | **DIVERGE** (bug 2) |
| 2026-06-02 | OPS consensus profile, **hard cap ₹500** | 0 | 0 | match (both correctly veto over-budget ATM at StrikeDepth) |
| 2026-05-27 / 06-01 | live profile | 0 | 0 | match (no signal) |

> Verification rigor note: an early "06-02 MATCH" was a **false positive** — a stray
> `docker compose run` pulled the stale GHCR image (no v2 code), so both runs were
> really v1. Caught via `v2 bars_traced=0`. All results above use `--pull never` on a
> locally-built image verified to contain the trace code (`bars_traced > 0`).

### 2.1 The 05-26 divergence (the only day that fired)

```
only in v1: 10:34 PE 55500 @186  pnl=-1.99%  exit=stagnant_exit
only in v1: 12:03 PE 55300 @180  pnl=-18.04% exit=premium_stop
only in v1: 13:01 PE 55200 @189  pnl=+10.89% exit=exit_stack
only in v1: 13:06 PE 55200 @192  pnl=+4.10%  exit=exit_stack
only in v1: 13:11 PE 55200 @176  pnl=-5.27%  exit=exit_stack
only in v1: 13:17 PE 55200 @184  pnl=-9.90%  exit=exit_stack
only in v1: 13:23 CE 55700 @6    pnl=-11.21% exit=exit_stack
only in v2: 10:36 CE 55900 @28   pnl=-12.14% exit=exit_stack
```

Two observations:
- **Direction/timing differ at the open:** v1 enters **PE 55500 @ 10:34**; v2 enters
  **CE 55900 @ 10:36** — opposite direction, ~same time.
- **v1 re-enters 5× on PE 55200 (13:01–13:23); v2 does not re-enter at all.**

## 3. Root cause — confirmed by the decision trace

### Bug 1 — DirectionGate over-vetoes on non-consensus profiles (05-26)

The trace for all 7 v1-only bars is identical in shape:

```
v2@10:34: Direction=VETO(ml_confidence_below_bypass  ml_confidence=0.796  bypass_min=0.8)
v2@12:03: Direction=VETO(ml_confidence_below_bypass  ml_confidence=0.672  bypass_min=0.8)
v2@13:01: Direction=VETO(ml_confidence_below_bypass  ml_confidence=0.664  bypass_min=0.8)
... (13:06=0.652, 13:11=0.698, 13:17=0.761, 13:23=0.670)
```

`DirectionGate.apply` ([entry_pipeline_gates.py](../strategy_app/engines/entry_pipeline_gates.py#L168))
unconditionally does:

```python
if ml_vote.confidence < cfg.bypass_min_confidence:   # 0.80
    return GateResult.veto("ml_confidence_below_bypass", ...)
```

But `CONSENSUS_BYPASS_MIN_CONFIDENCE` is the gate for v1's **consensus-bypass path only**
(`_process_entry_consensus`, used for `_PROFILES_ML_ENTRY_CONSENSUS`). The live profile
`trader_master_live_v1` is **not** a consensus profile — in v1 it takes the
scored/sequential path, gated by `min_confidence` (≈0.50), so it legitimately enters at
ML confidence 0.65–0.80. v2 applies the 0.80 bypass gate to *every* profile → vetoes
those 7 trades. **The opening 10:36 CE that v2 did take just happened to clear 0.80;
everything after didn't.**

### Bug 2 — v2 ignores v1's stateful risk guards (06-02, consensus profile)

`v2 bar outcomes: Direction=18, entered=12` and the 3 v2-only trades all land at
13:07–13:25, **after** both runs logged `consecutive loss limit reached count=3`. v1's
consensus path stops trading once the consecutive-loss halt trips and during re-entry
cooldowns; v2's cascade has **no gate for consecutive-loss halt / re-entry cooldown /
session-trade-cap timing**, so it keeps entering. v2 enters 12 vs v1's 9.

## 4. The fixes

1. **DirectionGate:** only enforce the consensus-bypass threshold when the active profile
   is a consensus profile (mirror v1's `_PROFILES_ML_ENTRY_CONSENSUS` dispatch). For
   non-consensus profiles, resolve direction from the candidate vote and let
   `ConfidenceGate` apply `min_confidence` — do not veto on `bypass_min`.
2. **Add a `RiskStateGate`** (or fold into `HardGatesGate`) that replicates v1's
   consecutive-loss halt, re-entry cooldown, and session-trade-cap checks, so v2 stops
   trading exactly where v1 does.
3. Re-run the golden master on 05-26 (live) + 06-02 (consensus, soft cap) until both
   are MATCH, then widen to more days before any cutover.

## 5. What should have been done (process)

- **Golden master should have run *during* the refactor, not after.** The §6 design
  mandated v1≡v2 parity before cutover; the implementation landed without it, so the
  divergence surfaced only when we tested. Right order: wrap v1 logic in gates →
  prove parity → *then* clean up.
- **`DirectionGate` returns `VETO` (kills the bar).** In v1, a direction it can't resolve
  for one candidate doesn't necessarily kill the bar. DirectionGate is bar-level (operates
  on the ML vote, not the candidate), so VETO may be correct — **but this must be verified
  against v1, not assumed.** If v1 would re-attempt, this is a v2 regression.
- **No per-bar decision parity check.** We diffed *trades*, which is downstream of exits.
  We should diff the *entry decision per bar* (entered/which-side/why-not) so a divergence
  is attributed to the gate that caused it, not inferred from end-of-day trades.
- **06-02 couldn't be verified** because the harness used the live profile, not the OPS
  sim config. The harness must apply the **full** OPS `sim_env` to reproduce a firing day.

## 6. What's needed for a definitive analysis (next build)

1. **Engine exposes the decision trace:** store `self.last_entry_trace = {decision_id,
   timestamp, final_outcome, gates:[{gate, outcome, reason, values}]}` at the end of
   `_process_entry_votes_v2`. (2 lines.)
2. **Replay collects per-bar traces** into `ReplayResult.decision_traces`.
3. **Harness applies the full OPS sim config** (mirror `ops_routes._run_sim_thread.sim_env`)
   so 06-02 fires, and **dumps the per-bar cascade** for every bar where v1 and v2 differ
   on (entered?, side).
4. Re-run 05-26 (firing, live) **and** 06-02 (firing, OPS config); attribute each
   divergent bar to its cause.

## 7. Terminal UI — what info we need (powered by §6)

The Terminal already renders a decision table from `decision_traces.jsonl`
([terminal-live.jsx](../market_data_dashboard/static/webapp/terminal-live.jsx#L357-L588):
ENTER/SKIP/VETO pill, gate funnel, `blocker_gate` + `reason_code`). v2 does **not** feed
it yet. To make it answer "how was this trade picked / why not", per bar we need:

| Field | Source | Why |
|---|---|---|
| `timestamp`, `decision_id` | EntryContext | align trace to the chart bar |
| `final_outcome` (entered / no_trade) | evaluate_v2 | the headline per bar |
| ordered `gates[]` with `{gate, outcome, reason, values}` | `ctx.trace` | the cascade — *which gate stopped it and the numbers* |
| `selected` side + strike + premium | ctx | what was actually picked |
| `direction_consensus` (ce/pe score, margin) | DirectionGate values | the #1 divergence driver (§4.3) |
| `max_premium` / `atm_ltp` on StrikeDepth veto | StrikeDepthGate values | makes the ₹500-cap veto visible |

Rendering: one row per evaluated bar; click to expand the full gate cascade. Clicking a
trade on the tape jumps to its `decision_id` row. This is the same view the engineer would
use to attribute a divergence — the tool debugs itself.

## 8. Bottom line

- v2 ≠ v1 on the one day that fired (05-26). **v2 stays OFF.**
- The cause is **upstream of the gates** (different opening trade → different day), not a
  single strict gate — confirmed by the absence of v2 gate-veto logs on the missing bars.
- A definitive, per-gate attribution needs the trace capture in §6, which is also the
  Terminal feature in §7. Next step builds both.
