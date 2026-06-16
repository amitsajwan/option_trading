# Signal → Trade: the full flow

**Scope.** This is the *common* pipeline that turns a market snapshot into an open
position and then closes it. It is the same for every strategy mode. The
`Scalper / Adaptive / Lottery` selector in the OPS panel only changes the **exit**
stage (last section) — entry is mode-agnostic.

> Naming note: there is **no "adaptive" entry flow**. `adaptive` is one of three
> `EXIT_STRATEGY_MODE` values (`scalper` | `lottery` | `adaptive`). Do not name a
> full-flow document "adaptive" — it describes only the exit branch.

All paths run inside `DeterministicRuleEngine.evaluate(snap)`
([deterministic_rule_engine.py](../strategy_app/engines/deterministic_rule_engine.py)),
called once per snapshot.

---

## Stage 0 — Snapshot in

One 1-minute snapshot (futures bar + option chain + session context) arrives and is
wrapped in a `SnapshotAccessor` (`atm_strike`, `option_ltp`, `option_oi`,
`iv_percentile`, `strike_step`, …). Everything downstream reads from this accessor.

## Stage 1 — Regime + gates (can stop here)

- **Regime** is detected → `regime_signal` (BREAKOUT / TRENDING / SIDEWAYS / …).
- **Hard gates** — any of these → `return None` (no trade):
  - session phase not a valid entry phase, or risk paused
  - regime not allowed for the session tag (`is_session_regime_allowed`)
  - `ENTRY_TIME_WINDOWS` excludes the bar (leave **unset** — an E7 leftover once
    silently killed all candidates)
  - regime confidence < 0.60 (except relaxed profiles)

## Stage 2 — Strategy votes

Rule strategies + the `ML_ENTRY` timing vote each emit a `StrategyVote`
(`direction`, `confidence`, `raw_signals`). No vote → no trade.

## Stage 3 — Direction resolution (a veto point)

"Entry passed" means *timing/ML fired* — it does **not** guarantee a trade. The
direction must still be resolved and can be vetoed:

- **Consensus / bypass path** (`_process_entry_consensus`, ~L866):
  - needs an `ML_ENTRY` vote ≥ `CONSENSUS_BYPASS_MIN_CONFIDENCE`
  - `resolve_direction_consensus` fuses rule votes + shadow score + ML hint + regime
  - **`consensus.vetoed` or no direction → `return None`** ("if direction we are not
    sure, we cannot")
- **ML-resolves-conflict path** — scores candidates, keeps the eligible best.
- **Sequential path** — first ranked candidate that clears all gates.

## Stage 4 — Strike / depth selection (a veto point)

`_apply_strike_selection` → `select_strike`
([option_selector.py](../strategy_app/signals/option_selector.py)).
This stage is **part of the decision, not a cosmetic post-step**. It can say *no*:

- **IV too high** (`iv_percentile > SMART_STRIKE_IV_REJECT_PCTILE`) →
  `mode=rejected_high_iv`, strike `None`.
- **Premium budget** (`SMART_STRIKE_MAX_PREMIUM`):
  - Pass 1 picks the deepest OTM tier within budget that clears confidence / IV /
    regime / OI gates.
  - **HARD cap (default, `SMART_STRIKE_HARD_PREMIUM_CAP=1`)** — if no affordable,
    *priced* strike exists (incl. ATM over budget or with no LTP/depth) →
    `mode=rejected_premium_cap`, strike `None`.
  - SOFT cap (`=0`, legacy) — fall back to best/ATM even over budget.

When the selector returns a `rejected_*` veto, the engine flags the vote
(`_strike_vetoed`) and **every entry path skips it**, logging
`strike_veto … → no_trade`. (This is also why an over-budget ATM no longer trades.)

## Stage 5 — Entry policy + confidence

`_evaluate_entry_policy` → `EntryPolicyDecision.allowed`. Plus the hard
`confidence < STRATEGY_MIN_CONFIDENCE` gate. Either fails → skip the candidate.

## Stage 6 — Build the entry signal

`_build_entry_signal` (~L1051) is the final chokepoint. Returns `None` (no trade) if:
- direction not CE/PE
- **`_strike_vetoed` is set** (authoritative safety net)
- `atm_strike_only` policy violated, or no valid strike/premium

Otherwise it sizes lots (risk config) and emits a `TradeSignal(ENTRY)`.

## Stage 7 — Position opens

The tracker opens the position; `POSITION_OPEN` is persisted (JSONL canonical +
Mongo cache). `mfe/mae` tracking starts.

---

## Stage 8 — Exit (THIS is where the modes differ)

On every later snapshot the tracker runs the exit stack chosen at startup by
`build_default_exit_stack()` ([exit_policy.py:346](../strategy_app/position/exit_policy.py#L346)):

| `EXIT_STRATEGY_MODE` | Stack | Philosophy |
|---|---|---|
| `scalper` (default) | `build_scalper_exit_stack` | capture small gains, cut fast |
| `lottery` | `build_lottery_exit_stack` | lose small often, win big rarely; let winners run |
| `adaptive` | `RegimeAdaptiveExitPolicy` | **route by entry regime**: lottery on `ADAPTIVE_LOTTERY_REGIMES` (default BREAKOUT,TRENDING), scalper otherwise |

The first policy whose `check()` fires returns an `ExitReason` → `TradeSignal(EXIT)`
→ `POSITION_CLOSE` with `pnl_pct / mfe_pct / mae_pct / exit_reason`.

---

## One-line summary

```
snapshot → regime+gates → votes → DIRECTION (veto) → STRIKE/DEPTH (veto)
        → entry policy + confidence → build → POSITION_OPEN
        → [exit stack: scalper | lottery | adaptive] → POSITION_CLOSE
```

Two independent "no" gates after entry fires: **direction** (Stage 3) and
**strike/depth** (Stage 4). Both are real no-trades, both traced.
