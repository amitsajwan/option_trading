# 04 — Consolidated Backlog

*Epics, stories, tasks, and Definition of Done across all tracks. Priority-ordered.
Points are relative (Fibonacci). "DoD" at the bottom applies to every story unless a
story states extras.*

---

## Priority order (recommended)

1. **MD — Multi-Day Sim** (P0) — can't choose scalper vs lottery without it. [Doc 02](02_MULTI_DAY_SIM.md)
2. **STRIKE — Cheap-strike reach** (P1) — required for the genuine "lottery ticket" idea.
3. **MS — Multi-Strategy Platform** (P2) — future; design ready. [Doc 03](03_MULTI_STRATEGY_PLATFORM.md)
4. **EXEC — Execution hardening** (P1, parallel) — needed before any strategy goes live.
5. **OBS — Observability** (P2) — tuning + operator confidence.

---

## Epic MD — Multi-Day Sim  (P0)
*Full spec in [Doc 02](02_MULTI_DAY_SIM.md). Stories MD-S1…MD-S7 there. Summary:*

| Story | Pts | One-liner |
|---|---|---|
| MD-S1 | 3 | Extract shared `replay_engine.replay_day()` from `ops_routes._run_engine` |
| MD-S2 | 3 | Parquet multi-day snapshot loader |
| MD-S3 | 5 | Multi-day runner + aggregation (P&L, drawdown, histogram, profit factor) |
| MD-S4 | 3 | A/B harness (scalper vs lottery, same day set) |
| MD-S5 | 2 | Markdown report to `docs/reports/` |
| MD-S6 | 5 | OPS UI date-range mode + aggregate panel (if Option A) |
| MD-S7 | 2 | Fidelity validation (multi-day == live on overlapping day) |

**Epic DoD:** a reviewed report comparing scalper vs lottery over ≥20 days on cumulative
P&L *and* max drawdown, with a documented adopt/reject decision.

---

## Epic STRIKE — Reach genuinely cheap strikes  (P1)
*The lottery "₹400–800 ticket" idea needs deeper OTM than the selector supports.*

**STRIKE-S1: Honour `STRATEGY_STRIKE_MAX_OTM_STEPS`.** *(2)*
`option_selector._build_otm_tiers()` caps at OTM-4 and ignores the env. Make tiers
generate up to `STRATEGY_STRIKE_MAX_OTM_STEPS`.
- DoD: with steps=8 and budget=800 on a day with cheap deep strikes, sim selects a
  6–9-step OTM strike. Unit test for tier generation count.

**STRIKE-S2: OTM5–8 tier config.** *(2)*
Add `SMART_STRIKE_OTM5..8_{ENABLED,CONFIDENCE,IV_CEIL,REGIMES,MIN_OI}` with sane defaults
(confidence rises with depth, percentile IV ceilings per the §3.3 fix).
- DoD: config reference updated; defaults documented; sim shows tier selection by depth.

**STRIKE-S3: Liquidity reality check.** *(3)*
Investigate whether BANKNIFTY deep-OTM (₹400–800) has enough OI/volume intraday to fill.
Add a hard min-OI/volume gate per depth; skip illiquid strikes.
- DoD: a written finding (is the cheap-ticket idea even tradeable?) + a min-liquidity gate.

**STRIKE-S4: IV-ceiling percentile correctness test.** *(1)*
Lock in the §3.3 fix with a test: at iv_percentile=86, OTM tiers with percentile ceilings
89–92 pass; with the old 30–60 they'd reject.
- DoD: regression test prevents reverting to absolute-IV ceilings.

---

## Epic MS — Multi-Strategy Platform  (P2)
*Full design in [Doc 03](03_MULTI_STRATEGY_PLATFORM.md). Stories MS-S1…MS-S8 there.*

| Story | Pts | One-liner |
|---|---|---|
| MS-S1 | 3 | Strategy-aware namespacing (topics, run dir, run_id) |
| MS-S2 | 3 | Per-strategy consumer lock; N coexist |
| MS-S3 | 3 | Second strategy container (lottery), paper, own book |
| MS-S4 | 5 | Dashboard multi-strategy + portfolio view |
| MS-S5 | 8 | Portfolio risk manager (capital alloc + portfolio kill + per-instrument cap) |
| MS-S6 | 5 | Execution per-strategy tagging + fill attribution + reconciliation |
| MS-S7 | 3 | Per-strategy adapter selection (mixed paper/shadow/live) |
| MS-S8 | 3 | Config schema for "a strategy" (declarative) |

**Epic DoD:** see [Doc 03 §5](03_MULTI_STRATEGY_PLATFORM.md) — two strategies concurrent
in paper, 5 days, zero cross-talk; adding a third is config-only.

---

## Epic EXEC — Execution hardening  (P1, parallel)
*Built but unverified against real funds. Needed before live.*

**EXEC-S1: KiteAdapter integration test (paper API).** *(3)*
Verify NFO tradingsymbol construction, order placement, fill polling against Kite's
paper endpoint — not live funds.
- DoD: a placed order round-trips to a fill in a test; symbol format confirmed against
  a real instrument dump.

**EXEC-S2: Shadow-mode 5-day slippage study.** *(5)*
Run `EXECUTION_ADAPTER=shadow` (1 real lot + paper) for 5 days; measure real_fill −
paper_fill.
- DoD: slippage report; go/no-go for live per the cutover runbook.

**EXEC-S3: fill_tracker socket-timeout fix.** *(1)*
`fill_tracker` XREADGROUP block (5s) vs Redis socket timeout (2s) causes retry churn.
Use `socket_timeout=None` for the stream read.
- DoD: no timeout log churn over a session.

**EXEC-S4: Per-trade real P&L on dashboard.** *(3)*
Surface `fill_pnl_pct` + slippage on the trade inspector when real fills exist.
- DoD: a real (or shadow) fill shows real P&L distinct from simulated.

---

## Epic OBS — Observability & tuning  (P2)

**OBS-S1: Exit-reason analytics.** *(2)*
Per-session breakdown of exit triggers (which policy fired, P&L by exit reason).
- DoD: dashboard panel + the `exit_triggers` counter already in `session_stats()` surfaced.

**OBS-S2: Lottery parameter sweep tool.** *(3)*
Generalise the manual sweep (done ad-hoc this cycle) into a tool: vary
`LOTTERY_*` across a day/range, tabulate.
- DoD: one command sweeps timestop/runner/target and outputs the table seen in Findings §3.4.

**OBS-S3: Capture-ratio metric clarity.** *(1)*
"MFE capture" goes negative on losing sessions (correct but confusing). Add a tooltip /
doc note; consider also showing aggregate `Σpnl/Σmfe`.
- DoD: metric documented; no one mistakes it for a bug again.

---

## Definition of Done (applies to EVERY story)

1. **Merged** to `mordenization`, reviewed by ≥1 engineer.
2. **Tests** — unit tests for new logic; existing suite green
   (`strategy_app/tests/`, notably `test_exit_policy.py`, `test_risk_calculator.py`).
3. **Sim fidelity upheld** (Doc 01 §5): no writes to live state; config from
   `ops_env.json`/explicit overrides; ML lib versions pinned; profile risk_config merged.
4. **Config-driven**: any new tunable is an env var with a safe default, added to
   [Config Reference](05_CONFIG_REFERENCE.md). No hardcoded thresholds.
5. **Loose coupling**: new behaviour is a subscriber/publisher or a policy, not a
   cross-service import.
6. **Deployed + verified**: built on the VM, behaviour confirmed in a sim or live trace.
7. **Documented**: one-paragraph update to the relevant doc or a report in `docs/reports/`.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Lottery loses over many days (no tail) | Medium | High | MD epic decides with data before any live |
| Deep-OTM strikes illiquid (can't fill cheap tickets) | Medium | Medium | STRIKE-S3 liquidity study first |
| Sim diverges from live again (lib/config drift) | Medium | High | Fidelity rules + MD-S7 validation gate |
| Portfolio risk gap when multi-strategy live | Med | High | MS-S5 gates multi-strategy live |
| Kite slippage worse than modelled | Medium | Medium | EXEC-S2 shadow study before live |
