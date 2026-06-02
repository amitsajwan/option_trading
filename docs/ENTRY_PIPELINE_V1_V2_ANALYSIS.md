# Entry Pipeline v1 vs v2 — Divergence Analysis

**Status:** In progress. Verified facts + honest gaps below. Gate-level detail pending
trace capture (§6), which is also the Terminal feature (§7).

---

## 1. What was run

`golden_master_v1_v2.py` replays a day's real snapshots through the engine **twice** —
`STRATEGY_ENTRY_PIPELINE_V2=0` (v1, the three legacy paths) and `=1` (v2, the gate
cascade) — toggling **only** that flag, and diffs the resulting trades. Run inside the
live `strategy_app` container so models + libs match live.

## 2. Verified results

| Date | Config | v1 | v2 | Verdict |
|---|---|---|---|---|
| 2026-05-26 | live profile (`trader_master_live_v1`, min_conf 0.80) | **7 trades, −31.41%** | **1 trade, −12.14%** | **DIVERGE** |
| 2026-05-27 | live profile | 0 | 0 | match (no signal) |
| 2026-06-01 | live profile | 0 | 0 | match (no signal) |
| 2026-06-02 | live profile | 0 | 0 | match (no signal) |
| 2026-06-02 | partial OPS overrides (8 vars) | 0 | 0 | **INVALID** — config did not reproduce the 15-trade OPS sim |

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

## 3. The key diagnostic clue

On the 6 bars v1 traded but v2 didn't, **v2 logged no `entry_gate` veto/skip lines.**
If a gate had rejected those bars, we'd see them (every non-PASS is logged with a
`decision_id`). Their absence means those bars **never reached the gate cascade in v2** —
the divergence is **upstream of the gates**: vote pool, position-held short-circuit, or
re-entry/cooldown state.

> Therefore the headline is NOT "a gate is too strict." It is "v2 evaluates *fewer bars*
> than v1," most likely because of a different open decision (10:34 PE vs 10:36 CE) that
> cascades into different position/exit/re-entry timing for the rest of the day.

## 4. Root-cause hypotheses (ranked, to be confirmed by §6 trace)

1. **Different first trade → different day.** v2 picks CE@10:36, v1 picks PE@10:34. Once
   the opening trade differs, every subsequent position-held window, exit, and re-entry
   opportunity differs. A single different pick at the open can fully explain 1 vs 7.
   *Most likely.*
2. **Candidate ranking / VETO-vs-SKIP semantics.** v2 runs one ranked candidate per bar
   and a `VETO` kills the whole bar (vs `SKIP` → next candidate). If `DirectionGate`
   returns `VETO` where v1's consensus would have tried another candidate, v2 enters
   fewer bars. (Note: DirectionGate currently returns VETO, not SKIP — see §5.)
3. **Direction source mismatch.** v1 consensus vs v2 `DirectionGate` may resolve CE/PE
   differently at 10:34–10:36, producing the opposite opening side.

## 5. What's wrong / what should have been done

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
