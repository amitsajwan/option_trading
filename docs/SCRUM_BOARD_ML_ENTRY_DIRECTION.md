# Scrum board — ML entry · direction · trap detection

**Living document** — update status, owners, and **Results** after each replay / merge.  
**Last updated:** 2026-05-24 (E3-S6 CE AUC 0.540; direction ML ceiling confirmed; pivot to trap detection)  
**Profile under test:** `trader_master_ml_entry_v1` · **Engine baseline:** `a133936`

Related: [BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md](BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md) · [ENTRY_AND_DIRECTION.md](ENTRY_AND_DIRECTION.md) · [runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md](runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md)

---

## Strategic context (2026-05-24)

**Direction ML has hit a ceiling.** Three independent training attempts (unified S2, unified v2, dual CE/PE) all converge to AUC 0.54–0.56. This is not a training bug — it is a signal ceiling. Predicting "CE or PE" at session open from regime + velocity + IV features does not have enough information.

**The real edge is trap detection, not direction prediction.**
Profitable intraday option traders do not predict where price goes. They detect when one side is trapped and forced to unwind:
- Failed ORB breakdown → sellers trapped → CE squeeze
- Price below VWAP → retail puts in → snaps back above VWAP → PUT IV collapses → CE scalp
- PE IV spikes then compresses while spot holds → PE writers absorbing, not fleeing → trend continuation signal

These are observable market structure signals, not predicted outcomes. They produce cleaner labels, earlier entries, and are computable from the snapshot data we already collect.

**What this means for the board:**
1. E3 direction ML stories are closed/resolved — unified direction ML stays as interim signal (PF 2.21 vs momentum 0.57), but no more ML direction training investment
2. E4 (exits/TIME_STOP) is ungated — direction ML path found its ceiling, no need to wait
3. Epic E5 (trap detection) is the new sprint priority

**Known gaps to close:**
- 291% total MFE left on table across 61 trades — TIME_STOP fires at +0.19% while avg MFE was +9.6%
- Shadow scorer has 8 signals but none detect *failed* moves (the strongest real-world setup)
- Direction: AUC 0.54 unified ML stays interim until E5 trap signals prove out in replay

---

## How to use this board

1. Pick a story from **Backlog** → set **Owner** → move to **In progress**.
2. Check off **Tasks**; link run IDs in **Results log**.
3. When **Acceptance criteria** met, move to **Done**, paste metrics.

**Status:** `Backlog` | `Ready` | `In progress` | `In review` | `Done` | `Blocked` | `Cancelled`  
**Priority:** `P0` (blocking) · `P1` (this sprint) · `P2` (next sprint) · `P3` (later)

---

## Team roster

| Name | Role | Stories owned |
|------|------|----------------|
| _@name_ | **Ops / GCP** | E2-S6, E2-S8, E4-S2 — replays, results log, exit experiment |
| _@name_ | **Engine** | E5-S1, E5-S3 — trap signals, time-window weighting |
| _@name_ | **ML / research** | E5-S2, E5-S4 — intraday regime, dynamic exits |
| _@name_ | **Tech lead** | Review, sprint, board updates |

### Work packages (scripts)

| Team | Script | Purpose |
|------|--------|---------|
| Ops/GCP | `ops/gcp/run_engine_direction_ab.sh` | `baseline` \| `v1_direction_ml` \| `v1_dual_direction_ml` |
| Ops/GCP | `ops/gcp/run_oos_validation_replay.sh` | OOS windows |
| Ops/GCP | `ops/gcp/analyze_oos_validation_run.py` | PASS/FAIL analyzer |
| Engine | `ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh` | v1 profile + unified dir ML |
| ML | `ops/gcp/run_direction_dual_hpo_vm.sh` | E3-S6 dual bundle (complete when PE done) |

---

## Current sprint

| Field | Value |
|-------|--------|
| **Sprint** | Sprint 2 — Trap detection + exit quality |
| **Dates** | 2026-05-24 → TBD |
| **Sprint goal** | (1) TIME_STOP / MFE giveback experiment proves dynamic exit beats static target. (2) Failed-move trap signals in shadow scorer show measurable improvement in direction quality on replay. |

---

## Board snapshot

| ID | Story | Priority | Owner | Status | Points |
|----|-------|----------|-------|--------|--------|
| E1-S1 | ML_ENTRY primary voter in engine | P0 | | **Done** | 5 |
| E1-S2 | Document breakthrough + frozen config | P1 | | **Done** | 2 |
| E2-S1 | OOS runbook + analyze scripts | P1 | | **Done** | 3 |
| E2-S2 | Eval replay risk patch | P0 | | **Done** | 3 |
| E2-S3 | Brain skip flag for ML-entry eval | P1 | | **Done** | 2 |
| E2-S4 | Three-window validation (fair harness) | P0 | | **Done** | 5 |
| E2-S5 | Fix replay orchestrator (wait on run_id) | P1 | | **Done** | 1 |
| E2-S6 | Full Aug–Oct in-sample replay | P2 | Ops/GCP | **In review** | 3 |
| E2-S7 | Investigate May-only / low vote count | P1 | | **Done** | 5 |
| E2-S8 | 2023 parquet backfill | P3 | Ops/GCP | **Blocked** | 8 |
| E3-S1 | Tier 1 — CE guardrail / PE-only A/B | P0 | | **Done** | 5 |
| E3-S2 | Export + wire direction ML bundle | P1 | | **Done** | 5 |
| E3-S3 | Direction publish gate + A/B verdict | P1 | | **Done** | 3 |
| E3-S4 | Conditional S2 (entry-positive bars) | — | | **Cancelled** | 8 |
| E3-S5 | Profile `trader_master_ml_entry_v1` | P1 | | **Done** | 5 |
| E3-S6 | Dual direction model (CE + PE per-side) | P1 | ML | **Closed — ceiling** | 8 |
| E4-S1 | Session trade cap pilot (8 → 10) | P3 | | **Backlog** | 3 |
| E4-S2 | TIME_STOP / MFE giveback experiment | **P1** | Ops/GCP | **In Progress** | 5 |
| E4-S3 | Council exit layer (position re-eval) | P2 | Engine | **Backlog** | 8 |
| E5-S1 | Failed-move trap signals in shadow scorer | **P1** | Engine | **In Review** | 5 |
| E5-S2 | Intraday session regime classifier | P2 | ML | **Backlog** | 8 |
| E5-S3 | Time-window signal weighting | P2 | Engine | **Backlog** | 3 |
| E5-S4 | Dynamic exit — premium-based triggers | P2 | ML | **Backlog** | 8 |
| E5-S5 | Trap-aware direction ML (post-E5-S1) | P3 | | **Backlog** | 8 |

**Velocity (sprint 1):** 44 planned / 42 completed points  
**Sprint 2 capacity:** E4-S2 (5) + E5-S1 (5) + E2-S6 close-out (3) = 13 points  
**Sprint 2 velocity so far:** E5-S1 impl done (5) = 5 pts · E4-S2 in progress (v2 fix applied, re-replay needed)

---

# Epics and stories

---

## Epic E1 — ML_ENTRY integration ✓

**Outcome:** ML timing votes not vetoed by silent rules. In-sample breakthrough reproducible.

### E1-S1 — ML_ENTRY as primary voter · Done

- ML_ENTRY stays in vote pool; silence ≠ veto (`a133936`)
- Risk config preserved across ticks
- `no_selection` blocker: 1408 → 1
- Aug–Oct replay: 61 trades, PF 1.98, CE PF 1.93, PE PF 2.10

### E1-S2 — Breakthrough doc + frozen config · Done

- `docs/BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md`
- Frozen: `ENTRY_ML_MIN_PROB=0.65`, stop 20%, trail 35%

---

## Epic E2 — OOS validation & eval harness

**Pass bar (per window):** ≥40 trades · PF ≥1.30 · CE PF ≥1.00 · PE PF ≥1.00

### E2-S1 through E2-S5 · Done

All evaluation tooling, risk patches, brain-skip flag, three-window validation, and orchestrator fix complete.

### E2-S6 — Full Aug–Oct in-sample replay · In review

| | |
|--|--|
| **Status** | In review |
| **Owner** | Ops/GCP |
| **Points** | 3 |

**Best run:** `793f3a4d` — 146 trades, PF **1.19**, cap +7.7%, Aug+Sep only (no Oct). **FAIL** PF < 1.30.

**Open:** Oct month not present. Fresh `replay_only in_sample_sanity` on v1 + unified dir ML needed.

**Acceptance criteria**
- [x] ≥55 trades spanning Aug+Sep
- [ ] Oct 2024 month present in analyze output
- [ ] PF ≥ 1.30 (currently 1.19)

**Note:** With trap signals (E5-S1) added, re-run this replay to see if PF improves. Don't close until E5-S1 replay comparison done.

### E2-S7 — May-only OOS investigation · Done

Root cause documented: `session_summary.jsonl` carry pollution, `avoid_veto` dominating Aug–Sep, preflight force-recreate timing. Mitigations in place.

### E2-S8 — 2023 parquet backfill · Blocked

Secondary OOS `emitted=0`. Data pipeline ticket open: [docs/tickets/E2-S8_PARQUET_2023_BACKFILL.md](tickets/E2-S8_PARQUET_2023_BACKFILL.md).

---

## Epic E3 — Direction quality (CE vs PE) · Resolved

**Outcome:** Determine if direction ML adds value over rule-based direction. Answer: yes vs momentum, but ceiling is AUC ~0.55.

**Verdict (2026-05-24):** Three training attempts — unified S2, unified v2, dual CE/PE — all converge to AUC 0.54–0.56. This is the signal ceiling for "predict CE/PE at session open." The unified direction ML (`direction_only_model.joblib`) remains as the **interim direction signal** because it beats momentum (PF 2.21 vs 0.57) and is already deployed. No further direction ML training investment.

### E3-S1 — CE guardrail / PE-only A/B · Done

PE-only ML run: 16 trades, PF 0.92. CE still present from rule strategies. Decision: do not block CE at rule level; fix at profile level (E3-S5).

### E3-S2 — Wire direction ML bundle · Done

Exported `direction_only_model.joblib`. Wired `DIRECTION_ML_MODEL_PATH`. `avoid_veto` dominated on `det_dir_v1` profile (1135 vetoes) — fixed by E3-S5.

### E3-S3 — Direction publish gate + A/B verdict · Done

| | |
|--|--|
| **Status** | Done |
| **Decision date** | 2026-05-24 |

**A/B result (v1 profile, oos_primary, May 2024):**

| Variant | Trades | PF | Cap % | CE PF | PE PF |
|---------|--------|-----|-------|-------|-------|
| `direction_ml` (`ae5a86b7`) | 48 | **2.21** | +12.3 | 0.69 | 1.42 |
| `momentum` (`0eda153a`) | 44 | 0.57 | -5.6 | 0.12 | 1.10 |

**Decision:** Direction ML materially beats momentum. **Keep `DIRECTION_ML_MODEL_PATH` as interim.** CE PF 0.69 < 1.0 — CE leg losing, not publishable for live. The gap is not model quality (AUC 0.55) — it's that direction ML at session open cannot see *which side will be trapped intraday*. That requires trap signals (E5).

### E3-S4 — Conditional S2 train · Cancelled

**Cancelled 2026-05-24.** Training direction ML only on entry-positive bars would improve sample quality but won't escape the AUC ceiling. The feature set (regime + velocity + IV at open) does not contain enough intraday trap information. E5-S1 addresses the root cause.

### E3-S5 — `trader_master_ml_entry_v1` profile · Done

V1 profile removes TRADER_COMPOSITE/OI_UNWINDING `avoid_veto` conflicts. `avoid_veto` 1135 → 0. Valid run: `ae5a86b7`, PF 2.21.

### E3-S6 — Dual direction model · Closed — ceiling confirmed

| | |
|--|--|
| **Status** | Closed — ceiling confirmed |
| **Decision date** | 2026-05-24 |

**CE model result:** AUC **0.540** (CV: 0.499 ≈ random). Oracle sim: PF 0.46, 100% one-side imbalance. Research publish gate: **HOLD** (publishable: false).

**PE model:** Still training (~1h remaining as of decision). **Decision: do not run replay regardless of PE AUC.**

**Why closed:** CE AUC 0.540 is *worse* than the unified model (0.557) it was meant to improve. Both per-side models will land in the same 0.50–0.56 band as three prior attempts. The dual architecture is correct in theory but the feature set (`fo_direction_entry_context_v1`: regime + velocity + IV + OI + oracle rolling win rates at open) does not contain the intraday trap information needed to distinguish a profitable CE day from a profitable PE day.

**What to do with PE result:** Log the AUC when PE finishes. If PE AUC < 0.55: confirm ceiling, archive. If PE AUC ≥ 0.58 (would be surprising): reopen with focused replay. Do not export dual bundle or run `v1_dual_direction_ml` replay until PE shows meaningful signal.

**Unified `direction_only_model.joblib` remains active** — it beats momentum (PF 2.21 vs 0.57) and is already deployed.

---

## Epic E4 — Risk & exits

**Gate removed 2026-05-24.** E3 direction ML path has reached its ceiling. The 291% MFE left on table is a confirmed, measurable gap that does not depend on direction quality.

### E4-S1 — Session trade cap pilot (8→10) · Backlog P3

Low priority. Current `session_trade_cap` blocker is not top-2. Revisit after E4-S2 shows exit quality improvement.

### E4-S2 — TIME_STOP / MFE giveback experiment · In Progress P1

| | |
|--|--|
| **Status** | In Progress — v1 failed; v2 fix deployed, re-replay pending |
| **Priority** | P1 |
| **Owner** | Ops/GCP |
| **Points** | 5 |
| **Commits** | `c009a4e` v1 · `db80db5` v2 fix — 2026-05-24 |

**User story:** As a trader, I want to stop giving back MFE at TIME_STOP so that winners reach their natural exit instead of being cut at a flat P&L.

**Context:** Across 61 trades (run `3d1e2d1c`):
- TIME_STOP exits: 70% of all exits
- Avg P&L at TIME_STOP: **+0.19%**
- Avg MFE at TIME_STOP bars: **+9.6%**
- Total MFE given back: **291% across 25 trades** (pre-fix set; directionally similar post-fix)
- Trailing fires at avg +14.5% even with 35% activation config → likely compounding with TIME_STOP pattern

**Root cause:** Current exit config: fixed TIME_STOP (12 bars), fixed target (70%), fixed trailing (35% activation, 8% offset). No bar-by-bar re-evaluation of whether the reason to be in the trade still holds.

**Acceptance criteria**
- [ ] At least one replay with dynamic stagnant exit: exit only if *both* (a) below TIME_STOP P&L AND (b) momentum has flattened (e.g., shadow score crossed zero or below entry level)
- [ ] Replay shows TIME_STOP exits drop from 70% to < 50% of exits
- [ ] Target hits (TARGET_HIT + TRAILING_STOP at MFE ≥50%) increase
- [ ] PF improvement ≥ 0.20 vs `3d1e2d1c` baseline on same Aug–Oct window

**v1 replay result — FAIL (run `8c0b0ec0`, 2026-05-24, oos_primary May–Jul 2024):**
- Trades: 36 · PF: **0.74** · Cap: -3.5% · TIME_STOP: 21 exits, wr=14%, avg=-6.5%
- Root cause: held losing trades while shadow still agreed with direction → losses compounded
- TRAILING_STOP good (10 trades, 100% WR, avg +10.6%) but masked by loser overhang

**v2 fix (`db80db5`):** `_is_stagnant_exit` now exits immediately if `pnl_pct <= 0`; shadow gate only defers exit for profitable-but-stagnant trades.

**Tasks**
- [x] Implement `stagnant_exit_condition: shadow_score_crossed_zero` in risk config
- [x] Add replay config variant: `dyn_exit` (profile `trader_master_ml_entry_v1_dyn_exit`)
- [x] Run v1 replay → FAIL; diagnose root cause
- [x] Fix: add P&L floor — only defer exit when `pnl_pct > 0` (`db80db5`)
- [ ] Re-run replay (v2): `git pull && sudo bash ops/gcp/run_engine_direction_ab.sh dyn_exit`
- [ ] Accept: TIME_STOP% < 50%, PF ≥ +0.20 vs baseline, TIME_STOP avg P&L > 0

**Quick experiment first (2 days):** Change `stagnant_exit_bars` from 12 → 20 (less aggressive). Does PF improve? This is a 30-min config change + replay.

### E4-S3 — Council exit layer (position re-eval every bar) · Backlog P2

| | |
|--|--|
| **Status** | Backlog |
| **Priority** | P2 |
| **Points** | 8 |

**User story:** As a quant, I want the engine to re-evaluate open positions on every bar using the same signal council that opened the trade, exiting when the council flips.

**Design:**
- On every bar while a position is open: recompute shadow scorer + direction vote
- Exit trigger: shadow score crosses zero AND stays crossed for ≥2 bars (de-bounced)
- Exit trigger: ML entry prob drops below 0.40 for ≥3 consecutive bars (conviction decay)
- Soft target: if position has ≥30% MFE and conviction drops, exit half immediately (partial exit)

**Depends on:** E5-S1 trap signals (shadow scorer must have trap signals to make council exit meaningful) + E4-S2 validating the problem is real.

---

## Epic E5 — Trap Detection & Market Microstructure (NEW)

**Outcome:** Shadow scorer detects when one side is trapped and forced to unwind — the real intraday edge. Direction quality improves from AUC 0.55 → measurable replay PF gain without ML retraining.

**Core insight:** Retail traders see "price moved." Professionals see who is trapped, who must exit, where hedging pressure comes from. The signals for this are observable in real time from premium behavior, failed breakout/breakdown, and OI absorption — not predicted from session-open features.

**Five-layer framework:**
```
Layer 1 — Intraday regime (trending / mean-revert / gap-continue / expiry-squeeze)
Layer 2 — Trap detection (failed breakdown, failed breakout, IV trap, OI absorption)
Layer 3 — Option flow confirmation (CE/PE writing, premium acceleration, delta imbalance)
Layer 4 — Entry timing (ML entry AUC 0.83 — keep as-is)
Layer 5 — Dynamic exit (premium slowing, opposite side writing, VWAP loss, delta exhaustion)
```

Layers 4 is done (E1). Layers 2, 3, 5 are partially in the shadow scorer but missing the *failed move* detection. Layers 1 and 5 are new.

### E5-S1 — Failed-move trap signals in shadow scorer · In Review P1

| | |
|--|--|
| **Status** | In Review — implementation + tests done, replay pending |
| **Commit** | `528ff11` — 2026-05-24 |
| **Priority** | P1 |
| **Owner** | Engine |
| **Points** | 5 |

**User story:** As a trader, I want the engine to detect when a breakout or breakdown has failed and the trapped side is being forced to cover, so that the shadow scorer reflects the strongest real-world setup rather than pure directional momentum.

**New signals to add to `_shadow_direction_from_snapshot`:**

```python
# ── CE trap signals (bullish squeeze) ─────────────────────────────
"orb_low_rejected"     # price broke ORB low, now trading above it
                       # formula: prev_low < orb_low AND cur_close > orb_low
"vwap_reclaim_bull"    # price was below VWAP, reclaimed in last 2 bars
                       # formula: prev_close < vwap AND cur_close > vwap
"pe_iv_fading"         # PE IV spiked (>prev 2-bar avg) then compressed — trapped put buyers
                       # formula: pe_iv[-2] > pe_iv[-3]*1.05 AND pe_iv[-1] < pe_iv[-2]*0.97

# ── PE trap signals (bearish squeeze) ─────────────────────────────
"orb_high_rejected"    # price broke ORB high, now trading below it
                       # formula: prev_high > orb_high AND cur_close < orb_high
"vwap_reject_bear"     # price reclaimed VWAP, failed, now below again
                       # formula: prev_close > vwap AND cur_close < vwap
"ce_iv_fading"         # CE IV spiked then compressed — trapped call buyers
                       # formula: ce_iv[-2] > ce_iv[-3]*1.05 AND ce_iv[-1] < ce_iv[-2]*0.97
```

All computable from `snapshot.market_data` fields already collected.

**Acceptance criteria**
- [x] All 6 signals implemented in `_shadow_direction_from_snapshot`, appear in `shadow_basis` string
- [x] Unit tests: 20 tests covering all 6 signals + boundary conditions + buffer reset (`test_trap_signals_shadow_scorer.py`)
- [ ] Aug–Oct replay: run with new signals, check correlation — do trap signals fire on the 61-trade winners more than on losers? (Use `read_decision_timeline` + drill-down UI)
- [ ] PF on Aug–Oct with trap signals ≥ PF without (baseline: 1.98 on `3d1e2d1c`)

**Tasks**
- [x] Add signals to `deterministic_rule_engine.py` `_shadow_direction_from_snapshot` (rolling `_iv_buf` + `_pvwap_buf`, cleared on session start)
- [x] Update `_HM_SIGNALS` in `terminal-live.jsx` (ORB Trap, VWAP Trap, IV Fade rows)
- [x] Add unit tests in `strategy_app/tests/`
- [ ] Run replay: `sudo bash ops/gcp/run_engine_direction_ab.sh replay_only v1_direction_ml`
- [ ] Analyze: check `shadow_dir` vs actual outcome per trade; look at `shadow_basis` on winning vs losing days

### E5-S2 — Intraday session regime classifier · Backlog P2

| | |
|--|--|
| **Status** | Backlog |
| **Priority** | P2 |
| **Owner** | ML |
| **Points** | 8 |

**User story:** As a trader, I want the engine to classify the intraday session type in the first 15–30 minutes so it applies the right playbook — trend-following in trending days, mean-reversion in choppy days, premium selling in expiry compression.

**Five session types:**
| Type | Characteristics | Playbook |
|------|-----------------|----------|
| `trending` | ORB breaks clean, VWAP slope strong, PCR moves with price | Buy breakout, trail aggressively |
| `mean_reverting` | ORB break fails within 15 min, VWAP flat, PCR diverges | Fade the break, quick exit |
| `gap_continuation` | Gap ≥ 0.3%, first 5m candle extends gap direction, no fill | Ride momentum, no early exit |
| `expiry_compression` | Low IV range, OI accumulation at ATM, both CE/PE premium stable | Short straddle zone, avoid directional |
| `reversal` | Gap in one direction, first 30 min reverses completely | Trade against gap, longer hold |

**Input features (available at 9:45 IST):**
- Gap size and direction (overnight)
- First 5m and 15m candle range vs median range
- ORB high/low set by 9:30
- Whether price is above/below VWAP at 9:45
- CE/PE premium ratio direction in first 15m

**Implementation approach:**
1. Label historical sessions with session type (manual or heuristic rule)
2. Train lightweight classifier (LightGBM, ~15 features) — or use pure rules
3. Expose `session_type` on snapshot; shadow scorer adjusts weights per type

**Acceptance criteria**
- [ ] Session type classification correct on ≥70% of manually labeled days
- [ ] Replay with session-type-adjusted weights shows PF ≥ baseline on Aug–Oct
- [ ] `session_type` appears in decision traces

### E5-S3 — Time-window signal weighting · Backlog P2

| | |
|--|--|
| **Status** | Backlog |
| **Priority** | P2 |
| **Owner** | Engine |
| **Points** | 3 |

**User story:** As a quant, I want signals in the 9:25–10:00 institutional window to carry more weight than midday signals, reflecting observed time-of-day behavior patterns.

**Observed time-of-day behavior:**
| Window | Behavior | Signal weight |
|--------|----------|---------------|
| 9:15–9:25 | Noise discovery — high spread, fake moves | 0.5× (reduce confidence) |
| 9:25–10:00 | Real institutional direction — highest quality window | 1.5× |
| 10:30–11:30 | Trend continuation OR first reversal | 1.0× |
| 13:15–14:00 | Premium decay zone — theta drag, avoid long options | 0.7× |
| 14:00–15:00 | Late directional move — good for momentum entries | 1.0× |

**Implementation:**
- Add `_time_window_multiplier(snapshot_time) -> float` to engine
- Apply multiplier to `shadow_score` before threshold comparison
- Configurable via profile / env (not hardcoded)

**Acceptance criteria**
- [ ] Multiplier function implemented and tested
- [ ] Replay comparison: same window Aug–Oct with/without time weighting

### E5-S4 — Dynamic exit — premium-based triggers · Backlog P2

| | |
|--|--|
| **Status** | Backlog |
| **Priority** | P2 |
| **Owner** | ML |
| **Points** | 8 |
| **Depends on** | E4-S2 (validate TIME_STOP is the problem), E5-S1 (shadow signals valid) |

**User story:** As a trader, I want the engine to exit a position when the option flow turns against it — not when a fixed bar count expires — so winners are held longer and losers cut before they deteriorate.

**Exit triggers (any one fires → exit):**
```
1. Opposite-side premium accelerates > 15% in 3 bars AND shadow score crosses zero
   (e.g., holding CE, PE premium surges → market pricing downside → exit CE)

2. PCR moves > 20% against position direction in 5 bars
   (e.g., holding CE, PCR rising sharply → put buying dominant → exit)

3. VWAP lost for 3 consecutive bars in position direction
   (e.g., holding CE, price below VWAP for 3+ bars → bullish thesis invalidated)

4. Shadow score conviction decay: was ≥ 2.0 at entry, now < 0.5 for 2+ bars
   (signals that fired on entry have all reversed → no remaining edge)
```

**Implementation:**
- `PositionTracker.should_dynamic_exit(snapshot) -> bool`
- Fires only when position has been open ≥ 3 bars (prevent noise at entry)
- Logs which trigger fired to `decision_traces` for analysis
- Configurable per profile: `DYNAMIC_EXIT_ENABLED=true/false`

**Acceptance criteria**
- [ ] At least 2 triggers implemented and testable
- [ ] Replay shows TIME_STOP exits drop from ~70% to <40%
- [ ] Winners held longer: avg MFE at exit improves vs baseline
- [ ] PF improvement ≥ 0.30 vs `3d1e2d1c` baseline

### E5-S5 — Trap-aware direction ML · Backlog P3

| | |
|--|--|
| **Status** | Backlog |
| **Priority** | P3 |
| **Depends on** | E5-S1 (trap signals in replay showing correlation) |

**User story:** As ML research, I want to train a direction model on *trap labels* (is a CE trap in progress) rather than *direction labels* (did CE go up), so the model learns the observable state that precedes directional moves.

**Why this might work where direction ML didn't:**
- Direction labels require predicting the future from session-open features → AUC ceiling ~0.55
- Trap labels describe the *present* market state (failed move in progress) → should be more learnable
- Trap signals (E5-S1) become the target, not the feature

**Gate:** Only pursue if E5-S1 replay shows trap signals correlate with ≥65% of winning trades. If signals work as rules, ML is optional. If signals are noisy on their own, ML can learn which combinations matter.

---

## Epic E6 — Coverage & data quality (future)

| ID | Story | Priority | Status | Notes |
|----|-------|----------|--------|-------|
| E6-S1 | 2025 live data collection (Kite) | P2 | Backlog | Required for honest 2025 OOS |
| E6-S2 | May–Jul OOS coverage fix (avoid_veto + date range) | P2 | Backlog | Investigate month-boundary cursor |
| E6-S3 | OI data quality audit | P3 | Backlog | OI signals in trap detection need audit |

---

# Results log

**Eval harness:** `patch_trader_master_eval_replay_env.sh` · consec=15 · session_trades=12 · `SKIP_BRAIN_GATE=true` · `ENTRY_ML_MIN_PROB=0.65`

| Run label | Run ID | Window | Trades | PF | Cap % | CE PF | PE PF | Pass? | Notes |
|-----------|--------|--------|--------|-----|-------|-------|-------|-------|-------|
| breakthrough_ref | _(session)_ | 2024-08→10 | 61 | 1.98 | +2.33 | 1.93 | 2.10 | — | Pre-OOS ref; `a133936` |
| oos_primary_v1 | `57e60de8` | 2024-05→07 | 64 | 0.56 | -9.7 | 0.24 | 1.22 | Fail | Live risk; May-only |
| oos_primary_v2 | `5104f59d` | 2024-05→07 | 30 | 0.77 | -1.8 | 0.47 | 1.16 | Fail | Eval harness; May-only |
| oos_secondary | `25cca50d` | 2023-05→07 | 0 | — | — | — | — | Skip | `emitted=0` no parquet |
| in_sample_v1 | `76e2dcaf` | 2024-08→10 | 56 | 1.26 | +4.3 | 1.28 | 1.24 | Fail | Aug 1–8 only |
| in_sample_v2 | `793f3a4d` | 2024-08→10 | **146** | **1.19** | **+7.7** | 1.19 | 1.19 | Fail | Aug+Sep; no Oct |
| oos_pe_only | `cfe3f5a7` | 2024-05→07 | 16 | 0.92 | -0.2 | 0.80 | 1.08 | Fail | ML PE-only; rules still CE |
| oos_dir_ml | `f6195884` | 2024-05→07 | 6 | 0.74 | -0.5 | 0.24 | inf | Fail | det_dir; `avoid_veto` 1135 |
| oos_v1_dir_ml | `ae5a86b7` | 2024-05→07 | **116** | **2.21** | **+12.3** | 0.69 | 1.42 | Partial | v1 + dir ML; CE PF <1 |
| oos_v1_momentum | `0eda153a` | 2024-05→07 | 44 | 0.57 | -5.6 | 0.12 | 1.10 | Fail | A/B vs dir ML — dir ML wins |
| _void_ R1/R2 | `0acd6aea`, `bbc85202` | — | 4–6 | — | — | — | — | Void | Wrong profile / stale env |
| **dyn_exit_v1** | `8c0b0ec0` | 2024-05→07 | 36 | **0.74** | -3.5 | 1.06 | 0.43 | **Fail** | E4-S2 v1; held losers; TIME_STOP avg -6.5% |

**Direction ML training log:**

| Run | Model | CV AUC | Holdout AUC | Oracle PF | Published? | Notes |
|-----|-------|--------|-------------|-----------|------------|-------|
| `direction_s2_only_hpo_v2_…` | Unified S2 | ~0.516 | ~0.557 | — | No | Unified CE-vs-PE label |
| `direction_dual_ce_hpo_v1_20260524_051452` | CE per-side | 0.499 | **0.540** | 0.46 | No | AUC worse than unified |
| `direction_dual_pe_hpo_v1_20260524_070018` | PE per-side | TBD | TBD | TBD | — | Still training as of 2026-05-24 |

**Active deployed direction model:** `direction_only_model.joblib` (unified S2, AUC 0.557). Interim until E5-S1 trap signals proven in replay.

---

# Definition of Done

- [ ] Code on `main` with PR reviewed
- [ ] VM deploy via git pull + historical rebuild if `strategy_app` changed
- [ ] Replay run ID + analyze PASS/FAIL in Results log
- [ ] Story Status + Owner updated in this doc
- [ ] No secrets in commits

---

# Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-05-23 | — | Board created; E1/E2 done; E3 ready; results from `oos_all2` |
| 2026-05-23 | — | E3-S1/S2/S5 done; E3-S3 A/B: dir ML wins; E2-S7 diagnose |
| 2026-05-23 | ML | E3-S6 label fix (`min_abs_return` 0.003→0.001); VM HPO re-run |
| 2026-05-24 | — | E3-S6 CE AUC 0.540 — direction ML ceiling confirmed; E3-S4 cancelled; E3-S6 closed; E4 ungated; Epic E5 added; Sprint 2 set |
| 2026-05-24 | — | E4-S2 + E5-S1 implemented; E4-S2 v1 replay FAIL (PF 0.74, held losers); v2 P&L floor fix (`db80db5`); re-replay pending |

---

# Next tasks (sprint 2 order)

1. **E4-S2 quick experiment (Ops, P1, 2 days)** — Change `stagnant_exit_bars` 12→20, run Aug–Oct replay. Does PF improve? This is the fastest measurable test of the TIME_STOP hypothesis.

2. **E5-S1 (Engine, P1, 3 days)** — Add 6 trap signals to `_shadow_direction_from_snapshot`. Unit tests. Run replay, check if trap signals fire on winning trades. Update heatmap signal categories in UI.

3. **E2-S6 close-out (Ops, P2)** — Fresh `replay_only in_sample_sanity` on v1 + dir ML. Get Oct month. If PF still < 1.30, close as-is and note trap signals may fix it in Sprint 3.

4. **E3-S6 PE result logging** — When PE finishes, log AUC in direction ML training log. If < 0.55 as expected: no further action. If ≥ 0.58: reopen discussion.

5. **E4-S3 / E5-S4 design spike (ML, P2)** — Design the dynamic exit trigger spec before implementation. Decide: rule-based triggers (E5-S4) vs council re-eval (E4-S3) — or both as composable layers.
