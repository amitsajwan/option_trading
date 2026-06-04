# Trader-perspective changes from the 2026-06-04 session postmortem

Source: live paper session `paper-20260604-093501-031b5ab4` (4/4 wins, all `ML_ENTRY`,
all SIDEWAYS). The algo executed correctly; the two real weaknesses were *structural*:
the regime engine under-calls trends (so winners get scalped at the 3% target and
fresh-trend re-entries get blocked), and it will take a low-conviction counter-trend
fade. These four changes address that. **All are env-gated and default to the current
(legacy) behaviour** — nothing changes until you flip the flags.

---

## #1 — Trend-aware regime (`strategy_app/market/regime.py`)

**Problem.** `_classify_trend_vs_sideways` needs `bull_score`/`bear_score ≥ 2.0` for
TRENDING. A clean multi-timeframe alignment (5m/15m/30m all up) only scores **1.4**, and
on a low-volume morning the weak-vol penalty (`vol_ratio < 1.0 → ×0.8`) *demotes* it to
**1.12** → SIDEWAYS. The 2026-06-04 09:50–10:08 up-leg (a real +275pt grind) was tagged
SIDEWAYS the entire time. Consequence: the adaptive exit never routed to the runner
stack, and the `sideways_returns_mixed` gate blocked the 10:40 re-entry into the next leg.

**Change.** Two new knobs:

| Env var | Default | Effect |
|---|---|---|
| `REGIME_TREND_ALIGNED_BONUS` | `0.0` | Extra score added to the aligned side when 5m/15m/30m agree. When > 0 it ALSO exempts the aligned side from the weak-vol ×0.8 demotion. **Recommended: `0.8`** → aligned grind = 2.2 → TRENDING. |
| `REGIME_TREND_SCORE_MIN` | `2.0` | The TRENDING threshold (was hardcoded). Alternative lever — e.g. `1.3`. |

Default (`0.0` / `2.0`) reproduces legacy classification exactly.

## #2 — Confirmed trends → runner exit (NO new code; consequence of #1)

`EXIT_STRATEGY_MODE=adaptive` (already set on the VM) + `ADAPTIVE_LOTTERY_REGIMES=BREAKOUT,TRENDING`
already route a TRENDING/BREAKOUT **entry regime** to the runner/lottery exit stack
(BigTarget 40%, RunnerTrail 20%-act/35%-give) instead of the scalper 3% target.

The wiring is intact and verified:
`regime → signal.entry_regime_name` (`deterministic_rule_engine.py:1428`)
`→ position.entry_regime` (`position/position_factory.py:99`)
`→ RegimeAdaptiveExitPolicy._stack_for` (`position/exit_policy.py:332`),
covered by `test_exit_policy.py::...TRENDING` (runner-trail at +0.25 pnl / +0.30 mfe).

So **once #1 correctly tags the trend, the winner-fragmentation disappears automatically**:
the position is held on a runner trail instead of being scalped at 3% and re-entered.
No code change required — only #1 enabling the TRENDING label.

## #3 — Trend-fade guard (`strategy_app/engines/deterministic_rule_engine.py`)

**Problem.** The 10:33 PE faded a market still **+0.1% above VWAP** (bullish day
structure) on a shallow 15/30m dip; futures V-reversed +200pt one bar after the exit.
The trade was saved only by a good exit, not a good entry.

**Change.** A new discipline gate (gate 6, mirrored in `_process_entry_votes`,
`_derive_entry_blocker`, and the trace renderer). It blocks a counter-trend option that
fades the dominant VWAP trend **while the opposing move is still a shallow pullback**, and
**releases once the reversal is a genuine 30m trend** so true reversals stay tradeable.

| Env var | Default | Effect |
|---|---|---|
| `TREND_FADE_GUARD_ENABLED` | `false` | Master switch. Off = no-op. |
| `TREND_FADE_GUARD_VWAP_MIN` | `0.001` | How far price must sit on the trend side of VWAP (0.1%) to count as a dominant structure. |
| `TREND_FADE_GUARD_R30M_STRONG` | `0.005` | If `|fut_return_30m|` exceeds this the reversal is "real" and the guard releases. |

Logic: block **PE** when `price_vs_vwap ≥ vwap_min AND fut_return_30m > -r30m_strong`;
block **CE** when `price_vs_vwap ≤ -vwap_min AND fut_return_30m < +r30m_strong`. With-trend
entries (e.g. CE above VWAP) are never touched. Emits trace gate `trend_fade_guard`.

## #4 — Strike-selection audit (NO change; confirmed intentional)

The 2026-06-04 trades bought ~200pt OTM (CE 54600 / PE 54200 vs ~54400 ATM). Audited
against the live config: `STRATEGY_SMART_STRIKE_ENABLED=1` with `OTM2` reachable in any
regime and `OTM3/OTM4` gated to BREAKOUT/TRENDING. In SIDEWAYS only OTM1/OTM2 are
reachable, so the selector correctly picked the **deepest affordable strike (2-OTM,
premium ~1010 < `SMART_STRIKE_MAX_PREMIUM=1300`)**. Working as designed — no bug.

Synergy note: once #1 starts tagging TRENDING, `OTM3` (3-OTM, cheaper, higher leverage)
also becomes reachable on those bars — an intended deepening, worth watching on first
live validation.

---

## Recommended rollout (flip in `ops/gcp/operator.env`, one at a time, validate per step)

```
REGIME_TREND_ALIGNED_BONUS=0.8     # #1 (keystone — also delivers #2 via adaptive exit)
TREND_FADE_GUARD_ENABLED=true      # #3
# #2 needs nothing beyond #1 (EXIT_STRATEGY_MODE=adaptive already set)
# #4 no change
```

Suggested validation: enable `REGIME_TREND_ALIGNED_BONUS` first, replay/observe that the
morning grind tags TRENDING and the runner stack holds the winner; then enable the fade
guard and confirm the counter-trend PE-type entries get blocked in the trace
(`primary_blocker_gate = trend_fade_guard`).
