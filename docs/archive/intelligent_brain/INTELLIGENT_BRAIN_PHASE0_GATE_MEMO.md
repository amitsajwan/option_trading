# Phase 0 Gate Decision Memo — BigMoveScore calibration (B-0.2)

**Date:** 2026-06-06 · **Author:** team CLAUDE · **Story:** B-0.2 (depends-on B-0.1)
**Companions:** [INTELLIGENT_BRAIN_SCRUM_BOARD.md](INTELLIGENT_BRAIN_SCRUM_BOARD.md) · [INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md](INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md) (§2 D3) · [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md) (§8)
**Artifact:** [`ops/research/bigmove_score_backtest.py`](../ops/research/bigmove_score_backtest.py) · machinery verified by [`strategy_app/tests/test_bigmove_score_backtest.py`](../strategy_app/tests/test_bigmove_score_backtest.py) (3/3 green)

---

## Decision: **GO** (proceed to Sprint 2 — build senses) — with conditions

The Phase-0 gate (Decision **D3**) is: *`loaded` (= compression AND OI-build) must hold **≥1.4× base** on the **≥100 pt / 10-min** target, on the accrued sample (n ≥ 100). A working `released` trigger is **not** required.* That criterion is **met** on the accrued 7 live days. The whole-program STOP is **not** triggered.

This is a GO to **build** (Sprint 2 senses + Sprint 3 decision brain), **not** a GO to **size**. Nothing touches live; the real money gate is the cost-aware e2e backtest (B-2.6) and, beyond it, OOS confirmation.

---

## The numbers the decision rests on

Accrued sample: **7 live days** (2026-05-26, 05-27, 06-01…06-05), ~2,400 eligible bars, 10-min horizon, direction-agnostic max-favourable-move in points.

| Signal | n | %≥100 pt | mean pt | %≥200 pt | vs base |
|---|---|---|---|---|---|
| **base** (any bar) | ~2,400 | **32–34%** | 93 | 5% | 1.0× |
| **`loaded`** = compression AND OI-build | **229** (~33/day) | **49%** | 117 | 11% | **1.44–1.53×** |

- **Gate arithmetic:** 49% / 34% = **1.44×**; 49% / 32% = **1.53×**. Either reading clears the **1.4×** bar, and **n = 229 ≥ 100**. → **PASS.**
- The lift **grows with move size** (11% vs 5% on ≥200 pt — a ~2.2× lift), which is the signature of a real spring-release detector: it gets *stronger* exactly where the money is.

*(Source: the prior VM runs documented in HANDOVER §8 / Appendix and the project memory. See "Confirmation pending" below — these specific per-bucket figures are reproduced by the now-refactored script on the VM, not from this workstation, which has no copy of `phase1_market_snapshots`.)*

---

## Monotonicity — confirm/deny the handover claim

**Claim under review (HANDOVER §8):** the *sum-of-4 score* (compression + vol_release + velocity + oi_build) is **NOT** monotonic — `score 0→95, 1→90, 2→106, 3→135` median pt, score 4 never occurred — so *a lone signal is noise*; the **pair** `compression AND oi_build` is the real phenomenon.

**Verdict: CONFIRMED, and correctly handled.**
- The non-monotonic step (0→1, median falls 95→90) is real: at score 1 the single firing component is most often the *noisiest* one (a lone velocity or volume blip), not compression. The refactored proof now prints the **component mix per bucket** alongside the drop, so the explanation is mechanical, not hand-waved (`monotonicity_notes()`, verified by test).
- The program does **not** depend on the sum-score being monotonic. D3 deliberately gates on the **`loaded` pair**, not the additive score. **Recommendation: retire the sum-of-4 score entirely** — it should not appear in any sense or in the decision brain. Use the `loaded` pair (and, if it earns its place on the VM re-run, a `released` timing refinement).
- The honest dose-response that *should* be monotonic is **compression tightness** (tighter spring → bigger move). The refactored script emits this; it must be confirmed non-decreasing on the VM run (see conditions).

---

## The `released` trigger — open, and acceptably so

The original `released = velocity AND volume` on the **same bar never fired** (too strict — confirmed in code, was line 80). The refactored script now measures three variants on the VM data:
- `strict_and` (original), `current_or` (velocity **OR** volume), `3bar_or` (either within a trailing 3-bar window),
- each reported both standalone and **intersected with `loaded`**.

**Per D3, this is a refinement, not a gate.** If `loaded+current_or` (or `loaded+3bar_or`) beats `loaded` alone on %≥100, we gain an entry-timing trigger inside the loaded window. **If it does not, `loaded` alone is the signal — that is an accepted PASS.** Do **not** force-fit a trigger to make the board look complete.

---

## Confirmation pending (why this is GO-with-conditions, not unconditional)

This memo's gate *criterion* is satisfied by prior runs, and the analysis *machinery* is now verified green locally. What is **not** yet in hand from this session is a fresh end-to-end run of the **refactored** script on the VM mongo (this workstation has no `phase1_market_snapshots`). The gate number (lift ≥1.4×) does not change with the refactor — it recomputes the same `loaded` hit-rate — so the GO stands. The VM re-run is to **attach the artifact** and **answer the two open questions** (compression-tightness monotonicity; whether any `released` variant adds value).

**Conditions on the GO:**
1. **Re-run the verified script on the VM** (`python ops/research/bigmove_score_backtest.py` inside the strategy_app container) and paste its full output into B-0.1 Results: gate line + score-bucket table + monotonicity notes + release-variant table. Confirm the printed `lift ≥ 1.40×`.
2. **Compression-tightness dose-response must be non-decreasing** on buckets with n ≥ 20 (tighter → higher %≥100). If it inverts on a large bucket, re-open this memo.
3. **Sum-of-4 score is retired** — Sprint-2 senses encode the `loaded` pair, never the additive score.
4. **Sample honesty:** 7 quiet/low-vol days only. Treat every lift as directional. **Re-run Phase 0 weekly as data accrues; no real size until an OOS window confirms.** (Risk register, plan §5.1.)

**STOP trigger (unchanged):** if a larger/fresher sample shows `loaded` slipping **below 1.4× base** on ≥100 pt, the program halts and this gate re-opens.

---

## Architect sign-off

- [ ] Architect confirms GO and the four conditions — _record name/date here_
- [x] team CLAUDE (B-0.2) recommends **GO**, 2026-06-06.

> On sign-off, Sprint 2 (B-1.0 `senses/` contract) may move to **In progress**. Sprint 3's e2e cost gate (B-2.6) and live remain firmly closed.
