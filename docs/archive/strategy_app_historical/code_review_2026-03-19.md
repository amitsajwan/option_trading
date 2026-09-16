> **Historical record — 2026-03-19 code review. Not maintained.**

# Strategy App Full Code Review - March 19, 2026

Status: Completed implementation with active fixes now tracked in code.
Scope: `deterministic_rule_engine.py`, `all_strategies.py`, `manager.py`, `redis_snapshot_consumer.py`, `strategy_router.py`.

## Review Legend
- Severity: `High`, `Medium`, `Low`
- Action: `Must-Fix`, `Should-Fix`, `Nice-to-Fix`
- Status: `Implemented`, `Partially implemented`, `Deferred`
- File/line anchors are 1-based references in current HEAD.

## Finding Registry

### F1 - Exit voting is not strategy-owned (core attribution defect)
- Severity / Action: `High` / `Must-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/deterministic_rule_engine.py:301-347`
  - `strategy_app/engines/deterministic_rule_engine.py:724-747`
- Evidence:
  - Exit candidates are collected globally for all strategies before selection.
  - In multi-strategy runs, any owned or cross strategy can close a non-owned position.
- Code delta:
  - `_process_exit_votes()` now builds an owned-only pool keyed by `position.entry_strategy` and evaluates it first.
  - Falls back to universal exit votes only when no owned exit is valid.
  - Non-owned exits still possible via configured cross-exit/helper paths, preserving intended exceptions.

### F2 - `EXPIRY_MAX_PAIN` entries are over-activated and unbounded
- Severity / Action: `High` / `Must-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/strategies/all_strategies.py:705-852`
  - `strategy_app/engines/strategies/all_strategies.py:924-934`
- Evidence:
  - Entry previously allowed frequent expiry-day distance triggers.
  - Additional confirmation/frequency controls were missing.
- Code delta:
  - Strategy now defaults to `enabled=False`.
  - Entry now requires max-pain guards (`vol_ratio >= min_vol_ratio`, enabled gate, per-day entry cap, expiry day/time window).
  - Added timeout exit via `ExitReason.TIME_STOP` after `max_bars_in_trade`.
  - Per-day entry cap now increments only when an entry vote is actually emitted.
  - Default strategy set excludes `EXPIRY_MAX_PAIN` explicitly.

### F3 - OI entry/exit frame mismatch (`r15m` entry, `r5m` exit) is noisy
- Severity / Action: `Low` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/strategies/all_strategies.py:194-205`
  - `strategy_app/engines/strategies/all_strategies.py:288-350`
- Evidence:
  - Entry uses 30m OI change + 15m return confirmation.
  - Exit uses 5m return directly and could trigger on short reversals.
- Code delta:
  - Added optional minimum hold time (`min_exit_hold_bars`) before OI exits.
  - Added configurable `exit_r5m_threshold` to reduce noise-triggered reversals.
  - Added contextual raw signals (`bars_held`, `r5m`) to exit votes.

### F4 - EMA crossover exits have weak confirmation/no hysteresis
- Severity / Action: `Medium` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/strategies/all_strategies.py:358-370`
  - `strategy_app/engines/strategies/all_strategies.py:451-487`
- Evidence:
  - Exit used immediate EMA crossback condition on any qualifying bar.
- Code delta:
  - Added `ema_exit_min_bars_held` requirement.
  - Added minimum EMA spread to price (`ema_exit_min_spread_pct`) before permitting exits.
  - Kept exit reason as `REGIME_SHIFT`, with richer vote metadata.

### F5 - EMA base confidence was below engine min confidence
- Severity / Action: `Medium` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/strategies/all_strategies.py:358-365`
  - `strategy_app/engines/deterministic_rule_engine.py:422-426`
- Evidence:
  - Strategy default `confidence_base` sat below default engine minimum entry floor.
- Code delta:
  - EMA `confidence_base` raised from `0.60` to `0.65`.

### F6 - Regime-shift streak increment path can advance during deferral
- Severity / Action: `Low` / `Nice-to-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/engines/deterministic_rule_engine.py:301-360`
  - `strategy_app/engines/deterministic_rule_engine.py:724-747`
- Evidence:
  - Previous flow incremented streak state before defer decision.
- Code delta:
  - `_accept_regime_shift_exit()` is now called only after defer decision is resolved.
  - If deferred, no streak increment is performed that bar.

### F7 - VIX halt cannot always recover when intraday change is missing
- Severity / Action: `Medium` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/risk/manager.py:209-266`
- Evidence:
  - Missing `vix_intraday_chg` could leave a halt condition uncleared.
- Code delta:
  - Added cooldown-based recovery path when `vix_intraday_chg` is `None` and halt remains active.
  - Resume path clears halt through the existing resume flag/timestamp flow after configured cooldown.

### F8 - Session transition can skip next session start if end callback fails
- Severity / Action: `Medium` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/runtime/redis_snapshot_consumer.py:147-161`
- Evidence:
  - Failure in `on_session_end` blocked `on_session_start` for new session in the same branch.
- Code delta:
  - Wrapped end callback in try/finally style flow so next-session init always attempts.
  - Session bookkeeping reset is now executed with new session start logic.

### F9 - Budget lot sizing ignores signal confidence
- Severity / Action: `Low` / `Should-Fix`
- Status: `Implemented`
- Primary anchors:
  - `strategy_app/risk/manager.py:185-207`
- Evidence:
  - Prior budget branches computed integer lots from notional only.
- Code delta:
  - Added confidence clamp multiplier (`0.5..1.0`) for budget and notional modes before caps.
  - Keeps risk-based branch behavior unchanged.

## Implementation Plan (Prioritized by Risk)

### Priority 0 - Must-Fix now applied (already implemented)
1. **P0-1: Strategy-owned exit gate** (`B1-OWNED-EXIT`)
   - Files: `strategy_app/engines/deterministic_rule_engine.py`
   - Finding: F1 (+ F6 defer-correctness)
   - Goal: restore attribution and prevent unintended position closure by non-owner strategies.
2. **P0-2: Expiry max pain control** (`B2-EXPIRY-SAFETY`)
   - Files: `strategy_app/engines/strategies/all_strategies.py`
   - Finding: F2
   - Goal: hard-disable default run-path and reduce runaway entries/holding with new gating and timeout.

### Priority 1 - Operational robustness
3. **P1-1: Resume reliability + session lifecycle resilience**
   - Files: `strategy_app/risk/manager.py`, `strategy_app/runtime/redis_snapshot_consumer.py`
   - Findings: F7, F8
   - Goal: prevent stuck halts and ensure new trading session bootstraps even after end-hook errors.

### Priority 2 - Exit behavior quality
4. **P2-1: Exit noise filters for OI and EMA**
   - Files: `strategy_app/engines/strategies/all_strategies.py`
   - Findings: F3, F4
   - Goal: reduce micro-reversal exits and prevent premature closings.

### Priority 3 - Parameter correctness and size model consistency
5. **P3-1: Alignment + confidence-aware lot sizing**
   - Files: `strategy_app/engines/strategies/all_strategies.py`, `strategy_app/risk/manager.py`
   - Findings: F5, F9
   - Goal: align defaults with engine filters and tie budget sizing to signal quality.

## Patch Sequence Map (for external review handoff)

Use this order to minimize risk and simplify dependency validation.

- **Bundle B1-OWNED-EXIT**
  - Findings covered: F1, F6
  - Target files: `strategy_app/engines/deterministic_rule_engine.py:301-360`, `:724-747`
  - Rationale: highest attribution impact and lowest cross-component coupling.

- **Bundle B2-EXPIRY-SAFETY**
  - Findings covered: F2
  - Target files: `strategy_app/engines/strategies/all_strategies.py:705-852`, `:924-934`
  - Rationale: controls systemic trade explosion source on expiry days.

- **Bundle B3-EXIT-QUALITY**
  - Findings covered: F3, F4
  - Target files: `strategy_app/engines/strategies/all_strategies.py:189-350`, `:451-487`
  - Rationale: reduces strategy-exit noise and improves hold consistency.

- **Bundle B4-STABILITY-HALT-SHOTGUN**
  - Findings covered: F7, F8
  - Target files: `strategy_app/risk/manager.py:209-266`, `strategy_app/runtime/redis_snapshot_consumer.py:147-161`
  - Rationale: prevents hard-to-recover stalls in live loops.

- **Bundle B5-LOTS-SIGNAL-CONSISTENCY**
  - Findings covered: F9, F5
  - Target files: `strategy_app/risk/manager.py:185-207`, `strategy_app/engines/strategies/all_strategies.py:358-365`
  - Rationale: consistency fix for sizing and confidence semantics.

## Traceability Matrix

| Finding | Severity | Action | Bundle | Status | Owner |
|---|---|---|---|---|---|
| F1 | High | Must-Fix | B1-OWNED-EXIT | Implemented | Strategy core |
| F2 | High | Must-Fix | B2-EXPIRY-SAFETY | Implemented | Strategy module |
| F3 | Low | Should-Fix | B3-EXIT-QUALITY | Implemented | Strategy module |
| F4 | Medium | Should-Fix | B3-EXIT-QUALITY | Implemented | Strategy module |
| F5 | Medium | Should-Fix | B5-LOTS-SIGNAL-CONSISTENCY | Implemented | Strategy + risk |
| F6 | Low | Nice-to-Fix | B1-OWNED-EXIT | Implemented | Strategy core |
| F7 | Medium | Should-Fix | B4-STABILITY-HALT-SHOTGUN | Implemented | Risk manager |
| F8 | Medium | Should-Fix | B4-STABILITY-HALT-SHOTGUN | Implemented | Runtime |
| F9 | Low | Should-Fix | B5-LOTS-SIGNAL-CONSISTENCY | Implemented | Risk manager |

## No further code changes identified in current pass

- Status: No additional code changes required for this review package.
- Next doc action (optional): add a short release note for deployment tracking.
