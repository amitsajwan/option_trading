# Scrum board — ML entry · direction · trap detection

**Living document** — update status, owners, and **Results** after each replay / merge.  
**Last updated:** 2026-05-26 evening (R1-S1 VIX audit PASS; R1-S2 replay attempted — blocked by missing canonical snapshots for IS years 2020-H2→2023; dataset hierarchy clarified and deprecated; `run_r1s_replay.sh` created)  
**Profile under test:** `trader_master_ml_entry_v1` · **Live profile:** `trader_master_live_v1` · **Engine baseline:** `a133936`

Related: [BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md](BREAKTHROUGH_ML_ENTRY_PRIMARY_VOTER_2026-05-23.md) · [ENTRY_AND_DIRECTION.md](ENTRY_AND_DIRECTION.md) · [runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md](runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md)

---

## Strategic context (2026-05-25 — arc conclusion)

**E1–E8 research arc is complete. Zero configs have survived OOS validation.**

| Layer | Verdict |
|-------|---------|
| Entry timing (AUC 0.65) | Real edge, not enough alone |
| Direction ML | AUC ceiling 0.54–0.557 — decisive null |
| CE-only filter | PF 1.36–1.42 — structural asymmetry, not enough alone |
| Time-window filter | Collapsed OOS |
| Regime filter (E8) | In-sample PF 3.00 → OOS PF 0.54 — overfit |

**Root cause:** Long-ATM weekly at 1-min entry bleeds theta regardless of signal. No filter is sufficient.

**v3 microstructure verdict (2026-05-19):** 11 OI/Vol/Premium features at the same horizon — 1 PASS in 24 cells. **Adding more features at 1-min horizon does not unlock edge.** Any new feature must clear an audit gate before joining the engine.

**Current pivot (Sprint 4): Direction Signal Discovery.**  
Live mode is now running on GCP (paper, no real orders). Per-minute snapshots persist with full 25-strike chain. Depth ladder starts persisting tomorrow. Before adding any new voter, run audit-first: prove the signal exists in real live data using pre-registered gates, then implement as **shadow voter**, then promote after 5+ days of positive shadow.

**Two pivots remain valid:**  
1. **Sell-side premium (R1S)** — only PASS-class edge found in arc. Pre-registered hypothesis frozen in `docs/R1S_SELLSIDE_HYPOTHESIS_2026-05-26.md`. See Epic R1.  
2. **Direction discovery from live data** — new this sprint. See Epic D1.

**Known gaps still open:**
- E7 OOS (CE + top-3 windows) — replay running on VM; expected to fail
- E6 CE-only clean re-run — docker-compose bug fixed; needs VM run
- E7-S3 — baseline PF replay with `trader_master_live_v1` profile (VM task)
- R1-S1 — VIX field audit (blocks R1 epic)

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
| Ops/GCP | `ops/gcp/run_exit_risk_experiments.sh` | **E1–E5** May–Jul replay grid — [runbook](EXIT_RISK_EXPERIMENTS_2026-05.md) |
| Ops/GCP | `ops/gcp/run_engine_direction_ab.sh` | `baseline` \| `v1_direction_ml` \| `v1_dual_direction_ml` |
| Ops/GCP | `ops/gcp/run_oos_validation_replay.sh` | OOS windows |
| Ops/GCP | `ops/gcp/analyze_oos_validation_run.py` | PASS/FAIL analyzer |
| Engine | `ops/gcp/patch_trader_master_ml_entry_v1_direction_ml_env.sh` | v1 profile + unified dir ML |
| ML | `ops/gcp/run_direction_dual_hpo_vm.sh` | E3-S6 dual bundle (complete when PE done) |

---

## Current sprint

| Field | Value |
|-------|--------|
| **Sprint** | Sprint 4 — Direction Discovery + Live Data Enrichment |
| **Dates** | 2026-05-26 → 2026-06-09 (2 weeks) |
| **Sprint goal** | (1) Audit-first direction signal validated from real live data using pre-registered gates. (2) Tier-1 cross-asset data (NIFTY, basis, block flow) ingested. (3) Shadow voter for top feature emitting non-trading votes by sprint end. **No new primary voter until audit gates pass.** |

**Sprint 3 outcome (closed 2026-05-26):** E7-S1, E7-S2, E7-S4, E7-S5 done = 14 pts. **Bonus live-mode delivery:** GCP VM switched from historical replay to live ingestion + paper trading. Headless TOTP token refresh installed (`b552e4c`). Depth_collector upgraded with 5-level ladder + Mongo persistence (`05784b4`). Dashboard live-chart auto-refresh bug fixed (`2258a5f`). 10-strike (JUN ATM±2) depth coverage active. **Today's live session:** 294 snapshots persisted with full 25-strike chain; zero signals fired (correct — IV percentile 99.2 + EXPIRY regime; IV_FILTER vetoed all 36 evaluations).

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
| E2-S6 | Full Aug–Oct in-sample replay | P2 | Ops/GCP | **Cancelled — arc superseded** | 3 |
| E2-S7 | Investigate May-only / low vote count | P1 | | **Done** | 5 |
| E2-S8 | 2023 parquet backfill | P3 | Ops/GCP | **Blocked** | 8 |
| E3-S1 | Tier 1 — CE guardrail / PE-only A/B | P0 | | **Done** | 5 |
| E3-S2 | Export + wire direction ML bundle | P1 | | **Done** | 5 |
| E3-S3 | Direction publish gate + A/B verdict | P1 | | **Done** | 3 |
| E3-S4 | Conditional S2 (entry-positive bars) | — | | **Cancelled** | 8 |
| E3-S5 | Profile `trader_master_ml_entry_v1` | P1 | | **Done** | 5 |
| E3-S6 | Dual direction model (CE + PE per-side) | P1 | ML | **Closed — ceiling** | 8 |
| E4-S1 | Session trade cap pilot (8 → 10) | P3 | | **Backlog** | 3 |
| E4-S2 | TIME_STOP / MFE giveback experiment (E1–E4 grid) | **P1** | Ops/GCP | **Cancelled — arc superseded** | 5 |
| E4-S2b | Direction consensus + fast exit (E5 replay) | **P1** | Ops/GCP | **Cancelled — arc superseded** | 5 |
| E4-S3 | **5-gate ship:** E6 CE-only + cost overlay (Gates 1–2) | **P0** | Ops/GCP | **Cancelled — arc superseded** | 3 |
| E4-S3b | Council exit layer (position re-eval) | P2 | Engine | **Backlog** | 8 |
| E5-S1 | Failed-move trap signals in shadow scorer | **P1** | Engine | **Done** | 5 |
| E5-S2 | Intraday session regime classifier | P2 | ML | **Cancelled — arc superseded** | 8 |
| E5-S3 | Time-window signal weighting | P2 | Engine | **Cancelled — arc superseded** | 3 |
| E5-S4 | Dynamic exit — premium-based triggers | P2 | ML | **Cancelled — arc superseded** | 8 |
| E5-S5 | Trap-aware direction ML (post-E5-S1) | P3 | | **Folded into D1-S2** | 8 |
| E7-S1 | Live depth feed side-channel architecture | **P1** | Engine | **Done** | 5 |
| E7-S2 | Live profile `trader_master_live_v1` | **P1** | Engine | **Done** | 2 |
| E7-S3 | Replay with depth signals (Aug–Oct baseline) | P2 | Ops/GCP | **Cancelled — no historical depth data** | 3 |
| E7-S4 | Wire depth_collector in docker-compose + env docs | P2 | Ops/GCP | **Done** | 2 |
| E7-S5 | Halt button backend endpoint | **P0** | Engine | **Done** | 5 |
| R1-S1 | VIX field audit — verify snapshot.vix in IS parquet | **P0** | Ops/GCP | **Done** | 1 |
| R1-S1b | Rebuild canonical snapshots for IS years 2020-H2→2023 | **P0** | Ops/GCP | **Backlog** | 3 |
| R1-S2 | IS replay (2020-Q3 → 2023-Q4) VIX filter Gate 1 | P1 | Ops/GCP | **Blocked** (R1-S1b) | 3 |
| R1-S3 | OOS-A replay (2024-Q1 + Q2) Gate 2 | P1 | Ops/GCP | **Blocked** (R1-S2) | 3 |
| R1-S4 | OOS-B replay (2024-Q3 + Oct) Gate 3 | P1 | Ops/GCP | **Blocked** (R1-S3) | 2 |
| R1-S5 | R1S engine story (only if all gates pass) | P2 | Engine | **Backlog** | 8 |
| D1-S1 | Direction-prediction audit framework | **P0** | ML / research | **Backlog** | 5 |
| D1-S2 | Audit chain-aggregate features (PCR, OI Δ, IV skew) | **P0** | ML / research | **Backlog** | 3 |
| D1-S3 | Audit depth-derived features (qty_imb, microprice) | P1 | ML / research | **Blocked** (3+ days depth data) | 3 |
| D1-S4 | Shadow voter for top-validated feature | P1 | Engine | **Blocked** (D1-S2/S3) | 5 |
| D1-S5 | Promote shadow → primary (5-day audit gate) | P2 | Engine | **Blocked** (D1-S4) | 3 |
| D1-S6 | VIX-regime gate on direction voter | P2 | Engine | **Blocked** (D1-S5) | 2 |
| D2-S1 | Ingest NIFTY 50 cash + futures | **P1** | Engine | **Backlog** | 3 |
| D2-S2 | Ingest NIFTY BANK cash + compute futures-spot basis | **P1** | Engine | **Backlog** | 2 |
| D2-S3 | Block-trade detection from last_quantity | P1 | Engine | **Backlog** | 3 |
| D2-S4 | Integration smoke test for live enrichment rollout | P1 | Team Claude | **Backlog** | 2 |
| D3-S1 | Greeks per strike (Delta/Gamma/Theta/Vega) | P2 | Engine | **Backlog** | 5 |
| D3-S2 | Gamma Exposure (GEX) profile | P2 | Engine | **Blocked** (D3-S1) | 3 |
| D3-S3 | Expand option chain in snapshot (25 → 50 strikes) | P2 | Engine | **Backlog** | 2 |
| D3-S4 | Session VWAP + Anchored VWAP in snapshot | P2 | Engine | **Backlog** | 2 |
| D4-S1 | Complete WebSocket tick collector | P3 | Engine | **Backlog** | 8 |
| D4-S2 | Cumulative Volume Delta (CVD) from ticks | P3 | ML / research | **Blocked** (D4-S1) | 3 |
| D4-S3 | Calendar spread (JUN vs JUL futures) | P3 | Engine | **Backlog** | 2 |
| OP-S1 | Headless TOTP auth + systemd timer | — | Ops/GCP | **Done** | 5 |
| OP-S2 | GCP VM switched to live mode + paper trading | — | Ops/GCP | **Done** | 3 |
| OP-S3 | depth_collector: 5-level ladder + Mongo persistence | — | Engine | **Done** | 5 |
| OP-S4 | Dashboard live chart auto-refresh fix | — | Engine | **Done** | 2 |
| OP-S5 | ATM±2 (10-strike JUN) depth coverage | — | Ops/GCP | **Done** | 1 |

**Velocity (sprint 1):** 44 planned / 42 completed points  
**Sprint 2 velocity:** E5-S1 done (5) = 5 pts · E4-S2 v2 replay pending  
**Sprint 3 velocity:** E7-S1 (5) + E7-S2 (2) + E7-S4 (2) + E7-S5 (5) + OP bonus (5+3+5+2+1=16) = **30 pts**  
**Sprint 4 planned:** R1-S1 + D1-S1 + D1-S2 + D2-S1 + D2-S2 + D2-S3 = **17 pts P0/P1 must-have** · D2-S4 integration verification = +2 support follow-up · D1-S3 + D1-S4 = +8 stretch (depth-data dependent)
**Sprint 4 in-session (2026-05-26):** R1-S1 DONE (1 pt). R1-S1b discovered as new blocker (3 pts). Dataset hierarchy docs + deprecation markers committed. `run_r1s_replay.sh` ready on VM.

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

## Epic E7 — Live Infrastructure ✓ (partial)

**Outcome:** Depth order-book data flows as a side-channel into the strategy engine in live mode. Replay is completely unaffected. Live profile is ready for paper-live execution.

**Core architectural decision:** Depth is NOT embedded in the snapshot schema (would break historical replay and require schema migrations). Instead it is a Redis side-channel: `depth:atm_ce:latest` / `depth:atm_pe:latest`, read once per bar in `evaluate()`, silently absent in replay.

### E7-S1 — Live depth feed side-channel architecture · Done

| | |
|--|--|
| **Status** | Done |
| **Commit** | `2026-05-25` |
| **Points** | 5 |

**What was built:**
- `strategy_app/market/depth_context.py` — `StrikeDepth` + `DepthContext` dataclasses
- `strategy_app/runtime/redis_depth_reader.py` — `RedisDepthReader`; reads from Redis, checks 30s staleness, returns `None` when absent
- `ingestion_app/collectors/depth_collector.py` — polls Kite REST every `DEPTH_POLL_INTERVAL_SEC` (default 5s) during market hours; writes `depth:atm_ce:latest` / `depth:atm_pe:latest`
- `DeterministicRuleEngine` — accepts optional `depth_reader`; reads depth once per `evaluate()`; stores in `self._current_depth_ctx`
- 4 new depth signals (15–18) added to `_shadow_direction_from_snapshot`:
  - `depth_ce_bid_dom` (+1.5): CE bid_qty > ask_qty × 1.5 — CE buyers pressing
  - `depth_pe_offer_dom` (+1.5): PE ask_qty > bid_qty × 2 — puts being sold (bullish)
  - `depth_pe_bid_dom` (−1.5): PE bid_qty > ask_qty × 1.5 — put buyers pressing (bearish)
  - `depth_ce_ask_dom` (−1.5): CE ask_qty > bid_qty × 2 — calls being dumped (bearish)
- `strategy_app/main.py` — `build_depth_reader_from_env()` wired into engine construction
- 26 tests in `strategy_app/tests/test_depth_signals.py` — all pass

**Activation (3 env vars):**
```bash
DEPTH_FEED_ENABLED=1
DEPTH_FEED_INSTRUMENTS=NFO:BANKNIFTY24AUG50000CE,NFO:BANKNIFTY24AUG50000PE
DEPTH_POLL_INTERVAL_SEC=5
```

**Replay:** `DEPTH_FEED_ENABLED` defaults to `0` — no depth keys in Redis → all 4 signals silent → zero impact on historical results.

### E7-S2 — Live profile `trader_master_live_v1` · Done

| | |
|--|--|
| **Status** | Done |
| **Points** | 2 |

**Profile spec vs paper (`trader_master_ml_entry_v1`):**

| Parameter | Paper | Live |
|-----------|-------|------|
| `stop_loss_pct` | 20% | **18%** (slippage is real) |
| `trailing_activation_pct` | 35% | **25%** (protect capital earlier) |
| `stagnant_exit_bars` | 12 | **10** (theta is real money) |
| `session_trade_cap` | 12 | **4** (until edge re-verified) |
| `atm_strike_only` | False | **True** (no OTM chase) |

Use with `ML_ENTRY_BLOCK_PE=1` (CE-only until PE OOS verified) and `DEPTH_FEED_ENABLED=1`.

### E7-S3 — Replay with depth signals (Aug–Oct baseline) · Backlog P2

Run Aug–Oct replay with `trader_master_live_v1` profile (but `DEPTH_FEED_ENABLED=0` — replay mode). Establishes the baseline PF for the live profile before depth upgrades. Compare to E2 `32b01989` May–Jul.

### E7-S4 — Wire depth_collector in docker-compose + env docs · Done

`depth_collector` service added to `docker-compose.yml` under `profiles: ["live"]`. Env vars: `KITE_API_KEY`, `KITE_ACCESS_TOKEN`, `DEPTH_FEED_INSTRUMENTS`, `DEPTH_POLL_INTERVAL_SEC`, `DEPTH_STALE_TTL_SEC`. Strategy_app gets `DEPTH_FEED_ENABLED` (default 0) and `DEPTH_STALE_SEC` (default 30).

**ATM rotation runbook** (embedded in compose comment):
1. Update `DEPTH_FEED_INSTRUMENTS` in `.env` with new strike symbols
2. `docker compose --profile live up -d --no-deps depth_collector`
3. Old keys auto-expire via Redis TTL (60s) — no manual cleanup needed

**Activation:**
```bash
DEPTH_FEED_ENABLED=1
DEPTH_FEED_INSTRUMENTS=NFO:BANKNIFTY25JUN50000CE,NFO:BANKNIFTY25JUN50000PE
docker compose --profile live up -d
```

### E7-S5 — Halt button backend endpoint · Done

`POST /api/operator/halt` endpoint implemented in `market_data_dashboard/routes/operator_routes.py`. Writes sentinel file; `strategy_app/risk/manager.py` checks `is_halted()` each tick. Path resolved via `STRATEGY_RUN_DIR` env var (set on both dashboard and strategy_app containers). Fixed in commits `5f450d6` (ro mount) + `5a3cbc5` (path resolver). Tests: `market_data_dashboard/tests/test_operator_halt_routes.py`.

---

## Epic R1 — Sell-side premium (R1S short ATM CE)

**Motivation:** Only PASS-class edge found in E1–E8 arc. Short ATM CE on ORB-down + bearish momentum + below VWAP; profits from theta capture in calm-vol regimes. Fails catastrophically in macro-vol events (STOP blowouts when IV spikes). Regime filter (VIX < 16) is the hypothesis.

**Spec:** `docs/R1S_SELLSIDE_HYPOTHESIS_2026-05-26.md` — pass gates and OOS windows are frozen.

| ID | Story | Priority | Owner | Status | Points |
|----|-------|----------|-------|--------|--------|
| R1-S1 | VIX field audit — verify `snapshot.vix` populated in IS parquet | P0 | Ops/GCP | **Done** | 1 |
| R1-S1b | Rebuild canonical snapshots for IS years 2020-H2→2023 | **P0** | Ops/GCP | **Backlog** | 3 |
| R1-S2 | IS replay (2020-Q3 → 2023-Q4) with VIX filter — Gate 1 | P1 | Ops/GCP | **Blocked** (R1-S1b) | 3 |
| R1-S3 | OOS-A replay (2024-Q1 + Q2) — Gate 2 | P1 | Ops/GCP | **Blocked** (R1-S2) | 3 |
| R1-S4 | OOS-B replay (2024-Q3 + Oct) — Gate 3 | P1 | Ops/GCP | **Blocked** (R1-S3) | 2 |
| R1-S5 | R1S engine story (only if all gates pass) | P2 | Engine | **Backlog** | 8 |

**Gate sequence:** R1-S1 ✅ → R1-S1b → R1-S2 (Gate 1) → R1-S3 (Gate 2) → R1-S4 (Gate 3) → R1-S5.
Stop immediately at first gate failure. Do not tune thresholds.

### R1-S1 — VIX field audit · Done

- **Result:** `vix/vix.parquet` 1182 rows, IS window 811 days, **100% fill rate** — PASS
- Script: `ops/gcp/audit_vix_field.py`
- VIX flows into canonical snapshots via `_compute_vix_block()` → `vix_context` block → `SnapshotAccessor.vix_current`

### R1-S1b — Rebuild canonical snapshots 2020-H2→2023 · Backlog P0

**Blocker discovered 2026-05-26:** The replay runner requires `parquet_data/snapshots/` with `snapshot_raw_json`. On the VM only 3 chunks exist: `2020-H1`, `2024-H1`, `2024-H2`. The full IS window (2020-07-01 → 2023-12-31) has **zero canonical snapshots** — hence every queued replay returns `emitted=0`.

`snapshots_ml_flat` and `snapshots_ml_flat_v2` may still have the IS data, but neither contains `snapshot_raw_json` — they are flat training/support tables only. They cannot drive the replay engine.

**What the next team needs to do:**
1. Identify source raw data for 2020-H2 → 2023 (MongoDB `phase1_market_snapshots_historical`, or re-run `snapshot_batch_runner.py` if OHLCV source parquets exist under `futures/`, `options/`, `spot/`)
2. Run `python -m snapshot_app.historical.snapshot_batch_runner --build-stage snapshots --min-day 2020-07-01 --max-day 2023-12-31` (or equivalent) to rebuild canonical parquet
3. Verify chunks appear: `parquet_data/snapshots/year=2020/chunk=202007_202012_m6/data.parquet` etc.
4. Then run: `sudo bash ops/gcp/run_r1s_replay.sh gate1`

**Script ready:** `ops/gcp/run_r1s_replay.sh` — profile `r1s_top3_paper_v1`, gates 1/2/3 and unfiltered baseline wired, `.env.compose` auto-patched, analysis called at end.

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

### Exit + risk grid (May–Jul 2024, full window) — [EXIT_RISK_EXPERIMENTS_2026-05.md](EXIT_RISK_EXPERIMENTS_2026-05.md)

**Harness:** `run_exit_risk_experiments.sh` · emit 2400/min · drain ≥400 closes · `ENTRY_ML_MIN_PROB=0.65`

| Exp | Run ID | Trades | PF | Jul cap | TIME_STOP | CE PF | PE PF | Verdict |
|-----|--------|--------|-----|---------|-----------|-------|-------|---------|
| Ref | `ae5a86b7` | 541 | 1.00 | −19.7% | 339 | 1.36 | 0.79 | Flat book |
| E1 stagnant_20 | `2b7cd0e7` | 491 | 1.03 | −16.2% | **230** | — | — | Best TIME_STOP cut |
| **E2 dyn_exit** | `32b01989` | 540 | **1.04** | **−15.0%** | 318 | 1.42 | 0.81 | **Best PF/Jul** |
| E2E3 stress | `cf5ce85a` | 309 | 1.02 | **−4.6%** | 179 | — | — | Thin book |
| E4 combo | `81d73382` | 484 | 1.03 | −19.2% | 219 | 1.27 | 0.89 | No beat E2 |
| **E5 consensus** | `2632cdc7` | 169 | 0.79 | −7.1% (May) | — | 134 | 0.50 | 0.89 | **Fail** | May-only; avoid_veto choke — [handover](HANDOVER_2026-05-22.md) |

**Loss anatomy (E2):** 41% WR; TIME_STOP 59% of trades (PF 0.15); PE leg PF 0.81; direction ML AUC ~0.55 ceiling.

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
| 2026-05-24 | Ops | E1–E4 May–Jul grid complete; best **E2** PF 1.04; E4 combo wash; replay throttle + drain fix (`ce02787`) |
| 2026-05-24 | Engine | Direction consensus profile `trader_master_ml_entry_consensus_v1` + E5 experiment (`013ae66`); [EXIT_RISK_EXPERIMENTS_2026-05.md](EXIT_RISK_EXPERIMENTS_2026-05.md) |
| 2026-05-24 | — | E5 done: `2632cdc7` PF 0.79, 169 trades May-only — expert handover updated; E5b + risk audit recommended |
| **2026-05-25** | Engine | **E1–E8 arc concluded: zero OOS configs.** E5-S1 trap signals marked Done (26 tests pass). Sprint 3 opened: live depth side-channel (E7-S1 + E7-S2) implemented — `DepthContext`, `RedisDepthReader`, `depth_collector`, 4 depth signals in shadow scorer, `trader_master_live_v1` profile. Replay fully unaffected. |
| **2026-05-26 evening** | Claude | **R1-S1 DONE.** VIX audit PASS (100% fill rate IS window). **R1-S1b discovered:** canonical `snapshots` parquet missing for IS years 2020-H2→2023 — `emitted=0` on every IS replay attempt. `snapshots_ml_flat` confirmed as flat-only (no `snapshot_raw_json`). Dataset hierarchy clarified: 3 datasets, deprecation comments added to `snapshot_access.py` + `snapshot_batch_runner.py`, `research_defaults.py` default fixed to `snapshots_ml_flat_v2`. Replay script `ops/gcp/run_r1s_replay.sh` created and on VM. R1-S2 remains blocked on canonical parquet rebuild. |
| **2026-05-26** | Ops/Engine | **Sprint 3 closed + Sprint 4 opened.** GCP VM switched from historical replay to **live ingestion + paper trading**. Headless TOTP refresh installed (b552e4c). depth_collector upgraded with 5-level ladder + Mongo persistence + 7-day TTL (05784b4). Dashboard live-chart auto-refresh fix (2258a5f). 10-strike JUN ATM±2 depth coverage active. Today's live session: 294 snapshots persisted with full 25-strike chain; zero trade signals (correct — IV percentile 99.2 + EXPIRY regime; IV_FILTER vetoed 36/36 evaluations). **Sprint 4 opens with Direction Discovery focus** — Epic D1 (audit-first signal discovery), D2 (Tier 1 data: NIFTY, basis, blocks), D3 (Tier 2: Greeks, wider chain, VWAP), D4 (Tier 3: WebSocket ticks). **Critical rule: no new primary voter until pre-registered audit gates pass.** |
| **2026-05-26** | Tech lead | **Sprint 3 closeout decisions.** 8 stories cancelled as "arc superseded" (E2-S6, E4-S2, E4-S2b, E4-S3 5-gate-ship, E5-S2, E5-S3, E5-S4, E7-S3) — all were predicated on the long-ATM-1-min lane that E1–E8 closed. E5-S5 folded into D1-S2 (trap signals are direction features). R1-S1 (VIX field audit) carried into Sprint 4 as the only genuine spillover — blocks the entire R1 sell-side epic. See "Sprint 3 closeout" section for full rationale table. |

---

# Next tasks (Sprint 4 order)

**Critical path: audit BEFORE implementing. Per v3 verdict, adding features at 1-min horizon without an audit is the failure pattern we must avoid.**

**Pull from Sprint 3 (genuine spillover):**

0. **R1-S1 VIX field audit (Ops/GCP, P0, 1 pt)** — Cheapest unblock available. Verify `snapshot.vix` is populated in IS parquet quarters on VM. Unblocks the entire R1 sell-side epic (R1-S2 → R1-S5).

**New Sprint 4 work — critical path:**

1. **D1-S1 Audit framework (ML, P0, 5 pts)** — Build the pre-registered audit script. Required before any direction-feature work can proceed.
2. **D1-S2 Audit chain-aggregate features (ML, P0, 3 pts)** — Run D1-S1 over today's 294 snapshots + accumulating days. Establishes baseline of which existing snapshot fields predict direction. **Includes the 6 E5-S1 trap signals as candidate features** (orb_low_rejected, orb_high_rejected, vwap_reclaim_bull, vwap_reject_bear, pe_iv_fading, ce_iv_fading).
3. **D2-S1 / D2-S2 / D2-S3 (Engine, P1, 3+2+3 pts)** — In parallel with audit: ingest NIFTY, basis, block flow. Each is a cheap independent ticket.
4. **D2-S4 Integration smoke test (Team Claude, P1, 2 pts)** — Verify the end-to-end path `ingestion_app -> snapshot_app -> Mongo -> downstream consumers` before Sprint 4 enrichment work is called done. Catch env wiring, field-shape, and audit-script mismatches early.
5. **Wait 3+ trading days for depth data accumulation** (no work, just elapsed time).
6. **D1-S3 Audit depth features (ML, P1, 3 pts)** — Once 3+ days of depth ticks accumulated.
7. **D1-S4 Shadow voter (Engine, P1, 5 pts)** — Implement top-validated feature as SHADOW only. Env-gated. Cannot trigger trades.
8. **Wait 5+ trading days for shadow data accumulation.**
9. **D1-S5 + D1-S6 Promote + VIX gate (Engine, P2, 5 pts)** — Only if shadow audit passes pre-registered gates.

---

# Sprint 3 closeout (2026-05-26)

The E1–E8 research arc concluded in Sprint 3 with a definitive structural verdict:
**"Long-ATM-1-min lane is exhausted. Structural pivot required."**
(Memory: `project_e7_oos_result_2026-05-25`, `project_e8_oos_failure_2026-05-25`.)

That verdict makes a cluster of in-flight or backlog stories no longer actionable, because they were all built on the dead lane. To keep the board honest, those nine stories were closed at Sprint 4 open. Documenting here so the closures aren't lost in the table churn:

| ID | Old status | New status | Why |
|----|---|---|---|
| E2-S6 | In review | **Cancelled — arc superseded** | Best run PF 1.19 already fails the gate; rerunning with traps doesn't change the structural conclusion |
| E4-S2 | In review | **Cancelled — arc superseded** | Exits don't fix the entry-side edge gap |
| E4-S2b | Done — needs E5b | **Cancelled — arc superseded** | E5b was implicitly cancelled with the arc |
| E4-S3 (5-gate ship) | In progress | **Cancelled — arc superseded** | Was the ship vehicle for the dead lane; renamed sibling story to E4-S3b to keep IDs unique |
| E5-S2 | Backlog P2 | **Cancelled — arc superseded** | Intraday regime classifier was a feature for the dead lane |
| E5-S3 | Backlog P2 | **Cancelled — arc superseded** | Time windows already failed OOS in E7D0 |
| E5-S4 | Backlog P2 | **Cancelled — arc superseded** | Dynamic exit on dead lane |
| E5-S5 | Backlog P3 | **Folded into D1-S2** | Trap signals ARE direction features — they're now part of the D1-S2 audit candidate list |
| E7-S3 | Backlog P2 | **Cancelled — no historical depth** | depth_collector is live-only; we have no historical depth backfill to replay against |

**Carried forward into Sprint 4:**

- **R1-S1 (VIX field audit, P0, 1 pt)** — the only Sprint 3 P0 leftover. Cheap. Unblocks R1-S2 → R1-S5.

**Still open from prior sprints, status unchanged:**

- **E2-S8 (2023 parquet backfill, Blocked, 8 pts)** — leave blocked; resurrect if R1 sell-side audit needs older quarters
- **E4-S1 (Session trade cap pilot 8→10, Backlog P3)** — defer past Sprint 4
- **E4-S3b (Council exit layer, Backlog P2)** — defer; revisit if D1 finds something
- **R1-S2 → R1-S5** — blocked on R1-S1, will unblock in order

---

# Epic D1 — Direction Signal Discovery

**Goal:** Identify a validated direction-prediction signal (CE vs PE) from real live data before adding any new voter to strategy_app. Audit-first, then shadow voter, then promote — no shortcuts.

**Strategic context:**
- E1–E8 arc closed: zero OOS edge from 1-min long-ATM lane
- v3 microstructure: 11 features, 1 PASS in 24 cells → **adding features without audit doesn't work**
- R1S sell-side: only PASS-class config so far (regime-conditional, VIX<16)
- Live mode shipped 2026-05-26: real data accumulating now
- This epic mandates: **no new primary voter until pre-registered audit gates pass**

### D1-S1 — Direction-prediction audit framework · Backlog · P0 · 5 pts

**Owner:** _unassigned_

**Description**

Build a reusable audit script that, given any candidate feature(s) from `phase1_market_snapshots`, measures predictive power for futures direction at 1m / 5m / 15m horizons.

**Tasks**
- [ ] Load snapshots from Mongo `trading_ai.phase1_market_snapshots` filtered by `trade_date_ist` range
- [ ] For each snapshot, extract feature value(s) and the futures_close at t+1m, t+5m, t+15m
- [ ] Compute hit-rate (sign match) per horizon
- [ ] Compute bootstrap 95% CI lower bound on hit-rate (1000 resamples)
- [ ] Compute by-day distribution: % of days with positive hit-rate
- [ ] Output table: feature → {hit_rate, ci_lb, pos_days_pct, n_obs, verdict}
- [ ] Verdict gate (pre-registered, frozen before any feature test): **CI lb > 50% AND pos_days_pct ≥ 60% AND n_obs ≥ 200**
- [ ] Save as `docs/audits/direction_audit_template.py` + example notebook

**Acceptance criteria**
- [ ] Runs against today's 294 snapshots and outputs verdict table
- [ ] At least 10 candidate features tested in the example
- [ ] Output reproducible (seed fixed)
- [ ] Anti-pattern guard: gates are written into the script as frozen constants, not parameters

**Anti-pattern callout:** Do NOT tune the gate to make a feature pass. Gates are frozen on PR open; any change requires a new ticket.

---

### D1-S2 — Audit chain-aggregate features · Backlog · P0 · 3 pts

**Owner:** _unassigned_ · **Dependency:** D1-S1

**Description**

Use the D1-S1 framework to test direction signal in features we already capture per minute. This is the cheapest signal discovery — no new data needed.

**Features to test** (all paths under `payload.snapshot.*`):
- `chain_aggregates.pcr`, `pcr_change_5m`, `pcr_change_15m`
- `chain_aggregates.ce_pe_oi_diff`, `ce_pe_volume_diff`
- `atm_options.atm_oi_ratio`
- `atm_options.atm_ce_oi_change_1m` − `atm_options.atm_pe_oi_change_1m`
- `atm_options.atm_ce_iv` − `atm_options.atm_pe_iv` (IV skew)
- `chain_aggregates.max_pain` distance from `futures_bar.fut_close`
- `chain_aggregates.distance_to_max_pain_pct`

**E5-S1 trap signals to audit as direction features** (folded in from cancelled E5-S5):
- `orb_low_rejected` — failed bear breakout, should predict CE-side recovery
- `orb_high_rejected` — failed bull breakout, should predict PE-side reversal
- `vwap_reclaim_bull` — price reclaimed VWAP from below, should predict CE
- `vwap_reject_bear` — price rejected at VWAP from above, should predict PE
- `pe_iv_fading` — PE IV compressing after spike, should predict CE
- `ce_iv_fading` — CE IV compressing after spike, should predict PE

Each trap signal has a documented direction tag — the audit tests whether that tag holds up at 1m/5m/15m horizons.

**Acceptance criteria**
- [ ] All listed features have verdict (pass/fail vs gates)
- [ ] Top 3 features by CI_lb identified
- [ ] Written to `docs/audits/CHAIN_FEATURES_DIRECTION_AUDIT_<YYYY-MM-DD>.md`
- [ ] **Note:** single-day n is too small to confirm pass; document this caveat. Repeat audit weekly.

**Important caveat:** 2026-05-26 (today) was EXPIRY-day regime with IV=99.2 percentile — NOT representative. Need at least 3 normal-regime days before drawing conclusions.

---

### D1-S3 — Audit depth-derived features · Blocked · P1 · 3 pts

**Owner:** _unassigned_ · **Dependency:** ≥3 trading days of data in `market_depth_ticks` (earliest available 2026-05-30)

**Description**

After 3+ trading days of depth ticks accumulated, audit the depth-derived features for direction signal — separately and in combination with chain-aggregate features (D1-S2).

**Features to test** (from `market_depth_ticks` docs):
- `qty_imbalance` (CE) − `qty_imbalance` (PE) at ATM strike, instantaneous
- Rolling 30s / 60s mean of CE−PE qty_imbalance
- `microprice` − `mid` (drift), rolling
- `spread` widening/tightening (z-score)
- Total bid stack: CE total_bid_qty − PE total_bid_qty
- OI-level interaction: depth imbalance × snapshot OI delta

**Acceptance criteria**
- [ ] At least 3 trading days of depth data in Mongo
- [ ] Audit run; verdict table written to `docs/audits/DEPTH_FEATURES_DIRECTION_AUDIT_<date>.md`
- [ ] **Incremental test:** does depth ADD signal on top of chain-aggregate features (D1-S2), or is it redundant? Use AUC of chain-only vs chain+depth.
- [ ] If redundant: document and DO NOT proceed to D1-S4 with depth

---

### D1-S4 — Direction shadow voter implementation · Blocked · P1 · 5 pts

**Owner:** _unassigned_ · **Dependency:** D1-S2 OR D1-S3 (at least one feature passes audit gates)

**Description**

Implement the top-validated feature as a **SHADOW voter** in strategy_app. Shadow = produces votes that are logged to Mongo but **do not drive trade execution**. This collects real-world performance data without risk.

**Tasks**
- [ ] New strategy class (e.g. `CHAIN_DIRECTION` or `DEPTH_DIRECTION`) in `strategy_app/engines/`
- [ ] Env gates:
  - `STRATEGY_SHADOW_DIRECTION_ENABLED=0` (default OFF)
  - `STRATEGY_SHADOW_DIRECTION_FEATURE=<feature_name>` (which validated feature to use)
  - `STRATEGY_SHADOW_DIRECTION_THRESHOLD=<value>` (from audit)
- [ ] Votes published to existing `market:strategy:votes:v1` topic with `shadow=true` flag
- [ ] **NOT registered in the regime router** — engine never sees shadow votes for entry decisions
- [ ] strategy_persistence_app already persists votes — confirm shadow flag is preserved
- [ ] Unit tests
- [ ] Tagged regression test: confirm no trade signals or position changes are triggered by shadow

**Acceptance criteria**
- [ ] Strategy class implemented + unit tests pass
- [ ] When `STRATEGY_SHADOW_DIRECTION_ENABLED=0`: no votes produced
- [ ] When `=1`: votes appear in Mongo with `shadow=true`
- [ ] At least one shadow vote captured during a live session
- [ ] Zero trade signals / position changes attributable to shadow

**Anti-pattern callout:** Even if the audit shows huge edge, this MUST go through shadow phase. No exceptions. Refer to v3 verdict.

---

### D1-S5 — Shadow → primary promotion · Blocked · P2 · 3 pts

**Owner:** _unassigned_ · **Dependency:** D1-S4 + 5 trading days of shadow data

**Description**

After 5+ trading days of shadow voting, run a second audit comparing the actual shadow votes against subsequent futures direction. If pass against pre-registered gates, register the strategy in the regime router so it becomes a primary voter.

**Pre-registered gates (frozen before audit run):**
- Hit rate > 55% with bootstrap CI lower bound > 50%
- ≥ 60% of trading days have positive hit-rate
- Holds in ≥ 2 of 3 horizons (1m, 5m, 15m)
- n_votes ≥ 100 over the 5 days

**Tasks**
- [ ] Audit script for shadow-vote vs futures-direction (extends D1-S1)
- [ ] Pre-register gates as frozen constants in PR description
- [ ] Run audit on accumulated shadow data
- [ ] If PASS: add strategy to regime router (`strategy_app/engines/strategy_router.py`)
- [ ] If PASS: flip `STRATEGY_SHADOW_DIRECTION_ENABLED` semantics (still env-gated, but now active)
- [ ] If FAIL: document, keep in shadow, or remove entirely

**Acceptance criteria**
- [ ] At least 5 trading days of shadow data
- [ ] Pre-registered gates documented
- [ ] Audit outcome documented in `docs/audits/SHADOW_PROMOTION_<date>.md`
- [ ] If passed: first live primary vote captured during paper session
- [ ] Memory entry created describing outcome

---

### D1-S6 — VIX-regime gate on direction voter · Blocked · P2 · 2 pts

**Owner:** _unassigned_ · **Dependency:** D1-S5 promoted

**Description**

Per `project_r1s_regime_finding`, signal works in calm regimes (VIX<16), fails in macro-vol events. Add a regime gate so the direction voter only fires in low-VIX conditions.

**Tasks**
- [ ] Gate condition: voter only fires when `vix_current < STRATEGY_DIRECTION_MAX_VIX` (default 16.0)
- [ ] Re-run D1-S5 audit split by VIX regime; document hit-rate difference
- [ ] If signal holds without VIX gate, consider removing — don't add complexity that doesn't help

**Acceptance criteria**
- [ ] VIX gate implemented + unit tests
- [ ] Audit shows meaningful separation between VIX<16 and VIX≥16 hit-rates

---

# Epic D2 — Live Data Enrichment (Tier 1)

**Goal:** Ingest cross-asset + flow data to enrich direction features. Cheap wins; high direction-signal value per academic + practitioner literature.

**Rationale:** Currently we only track BANKNIFTY futures. Options settle on the *index*, not futures — basis matters. NIFTY context provides market-wide trend. Block trades flag institutional flow.

### D2-S1 — Ingest NIFTY 50 cash + futures · Backlog · P1 · 3 pts

**Owner:** _unassigned_

**Tasks**
- [ ] Add `NSE:NIFTY 50` and `NFO:NIFTY26JUNFUT` to ingestion_app instrument list
- [ ] Snapshot integration: new field `nifty_context.{cash, future, basis, banknifty_minus_nifty_spread, spread_change_5m}`
- [ ] Compute spread z-score over rolling 60-bar window
- [ ] Unit tests

**Acceptance criteria**
- [ ] NIFTY cash + future in snapshot payload
- [ ] BANKNIFTY−NIFTY spread computed
- [ ] At least 1 trading day of data captured
- [ ] No breakage of existing snapshot consumers (persistence_app, strategy_app)

---

### D2-S2 — Ingest NIFTY BANK cash + basis · Backlog · P1 · 2 pts

**Owner:** _unassigned_

**Description**

Currently we track BANKNIFTY *futures* only. Options settle on the *index*. Futures-spot basis is a known FII-positioning indicator.

**Tasks**
- [ ] Add `NSE:NIFTY BANK` to ingestion
- [ ] Snapshot integration: `underlying_context.{cash, basis, basis_pct, basis_z}`
- [ ] basis = futures_price − cash_spot
- [ ] basis_z = rolling z-score (60-bar)
- [ ] Unit tests

**Acceptance criteria**
- [ ] `underlying_context` block in snapshot
- [ ] basis_z computed
- [ ] Available as candidate feature for D1-S2 audit

---

### D2-S3 — Block-trade detection from last_quantity · Backlog · P1 · 3 pts

**Owner:** _unassigned_

**Description**

Kite's quote response includes `last_quantity` (size of the last trade). Trades ≥ N lots flag institutional flow. Direction can be inferred: if last_price ≥ mid, the ask was hit (aggressive buyer); else bid hit (aggressive seller).

**Tasks**
- [ ] Extract `last_quantity` from quote response in ingestion path
- [ ] Lot size config per instrument (BankNifty option lot = 15 or 30 depending on contract)
- [ ] Detect block: `last_quantity >= N_LOTS * lot_size` (env: `BLOCK_TRADE_MIN_LOTS=5`)
- [ ] Tag direction from last_price vs mid
- [ ] Rolling 5-min counter: net block flow per instrument (block_buys − block_sells)
- [ ] Snapshot field: `block_flow.{atm_ce, atm_pe, futures}` with running net counter
- [ ] Unit tests covering: lot size resolution, direction tagging, rolling-window reset on session boundary

**Acceptance criteria**
- [ ] Block detection logic with configurable threshold
- [ ] Direction tagging unit-tested against known mid prices
- [ ] Snapshot includes block_flow object
- [ ] Available as candidate feature for D1-S2 audit

---

### D2-S4 — Integration smoke test for live enrichment rollout · Backlog · P1 · 2 pts

**Owner:** Team Claude

**Description**

Before Sprint 4 live-enrichment work is called done, run an end-to-end smoke test across `ingestion_app` -> `snapshot_app` -> Mongo -> downstream consumers. This is where env wiring, field-shape drift, and audit-script/schema mismatches should be caught before signoff.

**Tasks**
- [ ] Verify `/api/v1/market/tick/{instrument}` returns `last_quantity`, `best_bid`, `best_ask`, `mid` for NIFTY cash, NIFTY BANK cash, current BANKNIFTY future, and ATM CE/PE
- [ ] Verify persisted `phase1_market_snapshots` docs include `nifty_context`, `underlying_context`, `block_flow`, `futures_derived.vwap_anchored_open`, and `futures_derived.price_vs_vwap_anchored`
- [ ] Confirm env-gated activation path is documented and working: `CROSS_ASSET_ENABLED`, `NIFTY_FUT_SYMBOL`, `BLOCK_TRADE_MIN_LOTS`
- [ ] Confirm session-reset behavior on new `trade_date` for rolling spread/basis/block-flow state and anchored VWAP
- [ ] Confirm no breakage in downstream consumers (`persistence_app`, dashboard views, `strategy_app` snapshot parsers/accessors)
- [ ] Run `ops/gcp/audit_vix_field.py` and `ops/gcp/run_direction_audit.sh`; document any schema or default-path mismatches as explicit follow-up bugs before signoff
- [ ] Save results as `docs/audits/SPRINT4_INTEGRATION_SMOKE_<YYYY-MM-DD>.md`

**Acceptance criteria**
- [ ] At least one successful smoke run documented on target environment (local compose or GCP VM)
- [ ] Mongo snapshot sample shows all new fields populated, or explicitly null during warm-up windows
- [ ] No consumer errors attributable to the new fields
- [ ] Any audit-script/schema mismatch converted into explicit follow-up ticket(s) before sprint signoff

---

# Epic D3 — Live Data Enrichment (Tier 2)

**Goal:** Higher-effort data adds — Greeks, wider chain coverage, VWAP. Each independently has direction-signal value.

### D3-S1 — Greeks per strike (Delta/Gamma/Theta/Vega) · Backlog · P2 · 5 pts

**Owner:** _unassigned_

**Description**

We already compute IV per strike. Greeks are one Black-Scholes evaluation away. Adds delta-weighted OI (institutional positioning) and gamma exposure (dealer hedging magnets).

**Tasks**
- [ ] New module `snapshot_app/greeks.py` with Black-Scholes pricer
- [ ] Inputs: spot, strike, time-to-expiry, IV, rate, type (CE/PE)
- [ ] Output Greeks: delta, gamma, theta, vega per strike
- [ ] Aggregate fields in `chain_aggregates`: `net_delta_oi`, `net_gamma_oi`, `net_vega_oi`
- [ ] Unit tests against textbook reference values (known IV/spot inputs, within 1% of expected)
- [ ] Performance: full 25-strike Greek calc must complete within 50ms per snapshot

**Acceptance criteria**
- [ ] Greeks computed for all strikes in snapshot
- [ ] Aggregates in chain_aggregates
- [ ] Tests vs reference values pass within 1%
- [ ] Snapshot publication rate unchanged

---

### D3-S2 — Gamma Exposure (GEX) profile · Blocked · P2 · 3 pts

**Owner:** _unassigned_ · **Dependency:** D3-S1

**Description**

Dealer gamma exposure (GEX) per strike maps where market-makers need to delta-hedge. Net GEX flip point acts as a price magnet on high-OI strikes.

**Tasks**
- [ ] Per-strike: `gex = gamma × OI × 100 × spot²` (sign convention: CE +, PE −)
- [ ] Net GEX = Σ (CE_gex − PE_gex) across strikes
- [ ] Identify "GEX flip" strike where cumulative net GEX crosses zero
- [ ] Snapshot field: `gex_profile.{net_gex, flip_strike, top_3_pos_strikes, top_3_neg_strikes}`
- [ ] Unit tests

**Acceptance criteria**
- [ ] GEX profile in snapshot
- [ ] Flip strike correctly identified on test fixture
- [ ] Available as candidate feature for D1-S2 audit

---

### D3-S3 — Expand option chain in snapshot (25 → 50 strikes) · Backlog · P2 · 2 pts

**Owner:** _unassigned_

**Description**

Today snapshot includes 25 strikes around ATM. Wing strikes (deeper OTM) carry "tail bet" positioning that's directional. Bump to 50 strikes (±25).

**Tasks**
- [ ] Update strike-count config in snapshot_app
- [ ] Validate Kite quote API batch size still under limit (it is — 50 instruments is fine)
- [ ] Verify Mongo doc size doesn't push limits (each strike ~200 bytes × 25 extra = +5KB/snapshot, fine)
- [ ] Unit tests

**Acceptance criteria**
- [ ] 50 strikes per snapshot
- [ ] No regression in publication rate or Mongo write success rate

---

### D3-S4 — Session VWAP + Anchored VWAP · Backlog · P2 · 2 pts

**Owner:** _unassigned_

**Tasks**
- [ ] Compute session VWAP from session_open: cumulative (price × volume) / cumulative volume
- [ ] Anchored VWAP from configurable pivot (e.g., gap_fill, prior_day_close)
- [ ] Snapshot field: `vwap_context.{session, anchored, dist_pct, slope_5m}`
- [ ] Unit tests

**Acceptance criteria**
- [ ] Session VWAP in snapshot
- [ ] Distance from VWAP computed
- [ ] Available as candidate feature for D1-S2 audit

---

# Epic D4 — Live Data Enrichment (Tier 3)

**Goal:** Tick-level / high-frequency data. Significant architecture lift, deferred until lower tiers prove out.

### D4-S1 — Complete WebSocket tick collector · Backlog · P3 · 8 pts

**Owner:** _unassigned_

**Description**

`ingestion_app/collectors/websocket_tick_collector.py` exists but is partial. Complete it: subscribe to Kite WebSocket V3 in "full" mode for sub-second LTP + 5-level depth ticks. Replaces 5s REST polling for option depth (D4-S1 must coexist with depth_collector for back-compat).

**Tasks**
- [ ] Connect to Kite WebSocket V3 with reconnect/backoff
- [ ] Subscribe to ATM ± 2 strikes in "full" mode
- [ ] Persist ticks to new Mongo collection `market_ticks` with 3-day TTL
- [ ] Maintain Redis latest-tick keys for low-latency strategy_app reads
- [ ] Health metric: ticks/sec per instrument
- [ ] Integration tests + 1-day stability run

**Acceptance criteria**
- [ ] WebSocket connection stable for ≥6 market hours
- [ ] Sub-second tick rate observed
- [ ] depth_collector can be disabled when WS is healthy (env switch)

---

### D4-S2 — Cumulative Volume Delta (CVD) · Blocked · P3 · 3 pts

**Owner:** _unassigned_ · **Dependency:** D4-S1

**Description**

For each tick: classify as aggressive_buy if last_price ≥ prev_best_ask, aggressive_sell if ≤ prev_best_bid. Running cumulative net = CVD. Strong direction indicator in liquid markets.

**Tasks**
- [ ] Tick classifier in WebSocket consumer
- [ ] Running cumulative counter per instrument, session-resetting
- [ ] Snapshot field: `cvd.{atm_ce, atm_pe, futures, session_cumulative}`
- [ ] Tests

**Acceptance criteria**
- [ ] CVD computed per tick
- [ ] Session reset on market open
- [ ] Available as candidate feature for D1-S3 audit

---

### D4-S3 — Calendar spread (JUN vs JUL futures) · Backlog · P3 · 2 pts

**Owner:** _unassigned_

**Tasks**
- [ ] Add `NFO:BANKNIFTY26JULFUT` to ingestion
- [ ] Snapshot field: `calendar_spread.{value, change_5m, change_15m}`
- [ ] Unit tests

**Acceptance criteria**
- [ ] Both contracts polled
- [ ] Spread in snapshot
- [ ] Available as candidate feature for D1-S2 audit

---

# Epic OP — Live Ops (Sprint 3 bonus, all Done)

These were completed during Sprint 3 but track here for traceability.

### OP-S1 — Headless TOTP auth + systemd timer · Done · 5 pts

- `ingestion_app/kite_totp_auth.py` (3-step Zerodha headless login: connect/login → /api/login → /api/twofa → /connect/finish → access_token)
- `ops/gcp/install_token_refresh_timer.sh` (systemd timer fires daily at 03:00 UTC = 08:30 IST)
- 15 unit tests passing
- Commits: `b552e4c`, fix in `05784b4`
- Memory: `project_gcp_live_mode_2026-05-26`

### OP-S2 — GCP VM switched to live mode + paper trading · Done · 3 pts

- Cleared replay flag in Redis (`system:virtual_time:enabled`)
- Stopped `*_historical` containers
- Updated `.env.compose`: `STRATEGY_ROLLOUT_STAGE=paper`, `INSTRUMENT_SYMBOL=BANKNIFTY26JUNFUT`, `DEPTH_FEED_ENABLED=1`, plus `KITE_API_KEY` / `KITE_API_SECRET` (were missing — depth_collector needs them)
- Live profile + default services running (depth_collector, snapshot_app, persistence_app, strategy_app, strategy_persistence_app)
- Verified end-to-end: 294 snapshots persisted today, full 25-strike chain captured per minute

### OP-S3 — depth_collector: 5-level ladder + Mongo persistence · Done · 5 pts

- Full 5-level bid/ask ladder captured (was: best bid/ask only)
- Derived metrics: spread, mid, microprice, qty_imbalance, total stack qtys
- Mongo collection `market_depth_ticks` with indexes + 7-day TTL
- Redis writes preserved for back-compat with `RedisDepthReader`
- 17 unit tests passing
- Commit: `05784b4`

### OP-S4 — Dashboard live chart auto-refresh · Done · 2 pts

- Root cause: `LiveMongoSource` cached candle list once at WS-connect time; never refreshed when new minute bars arrived
- Fix: `get_latest_tick()` invalidates session cache when newer bar exists in Mongo
- `monitor_ws._loop()` pushes a full snapshot frame (not just tick) when candle count grows
- 2 regression tests added
- Commit: `2258a5f`

### OP-S5 — ATM±2 (10-strike JUN) depth coverage · Done · 1 pt

- `DEPTH_FEED_INSTRUMENTS` expanded from 2 → 10 strikes (BANKNIFTY26JUN55300-55700, CE+PE)
- Verified all 10 strikes return valid 5-level quotes
- depth_collector recreated with new config; clean startup confirmed
- Polling starts tomorrow morning 09:15 IST
- **Operator note:** May strikes were initially configured but all expire 2026-05-28 — switched to JUN strikes for liquidity
