# Exit System

The exit system is a single composable policy stack selected by `EXIT_STRATEGY_MODE`.
There is no second exit path. Legacy inline exits in the tracker are suppressed whenever
the stack is active (adaptive or lottery mode).

## Config (required in `.env.compose`)

```
EXIT_POLICY_STACK_ENABLED=1       # must be 1 — stack is the exit authority
EXIT_STRATEGY_MODE=adaptive       # adaptive | lottery | scalper
EXIT_MAX_LOSS_PCT=0.18            # universal floor, all modes/regimes
```

## How adaptive mode works

Every trade is routed at exit time by the regime it entered in:

```
BREAKOUT / TRENDING  →  LOTTERY stack  (let the trend run)
everything else      →  SCALPER stack  (cut fast, take small gains)
```

Override the lottery regime set: `ADAPTIVE_LOTTERY_REGIMES=BREAKOUT,TRENDING`

## Exit flow (each 5-min bar)

```
1. forced_exit_reason?         → exit (risk breach / broker forced)
2. risk.daily_loss_breached?   → RISK_BREACH exit
3. hard_close (15:20)?         → TIME_STOP exit
4. EXIT_MAX_LOSS_PCT floor      → STOP_LOSS if pnl <= -floor (always first)
5. regime-routed stack check   → see stacks below
6. soft_close (15:10)?         → TIME_STOP exit
```

## LOTTERY stack (BREAKOUT / TRENDING)

Tuned from June 2026 14-day sweep. Exit results: net +202% across 14 days; drop-top-6 = −20% (fundamental lottery structure).

| Policy | Config | Fires when |
|---|---|---|
| HardStop | `LOTTERY_HARD_STOP_PCT=0.18` | pnl ≤ −18% |
| ThesisFail | `LOTTERY_THESIS_FAIL_BARS=5` `LOTTERY_THESIS_FAIL_MIN_MFE=0.05` | at bar 5, MFE still < 5% — trade never moved, cut it |
| MomentumReversal | `LOTTERY_MOMENTUM_FLIP=1.0` | shadow_score flipped hard against direction |
| BigTarget | `LOTTERY_BIG_TARGET_PCT=0.99` | effectively disabled — let EOD capture the trend |
| RunnerTrail | `LOTTERY_RUNNER_ACTIVATION_MFE=0.25` `LOTTERY_RUNNER_GIVEBACK_FRAC=0.35` | MFE ≥ 25%; then floor = MFE × 0.65 (can give back 35%) |
| Timestop | `LOTTERY_TIMESTOP_BARS=90` | 7.5hr fallback — soft_close fires first |

Key: **no hard target**. The two trend days in June (06-23 +145%, 06-24 +33%) are where all P&L lives. An early target (50%) halves net. ThesisFail at bar 5 (not 3) gives slow-developing moves time to prove themselves.

## SCALPER stack (everything else)

| Policy | Config | Fires when |
|---|---|---|
| HardStop | `EXIT_SCALPER_HARD_STOP_PCT=0.18` | pnl ≤ −18% |
| ThesisFail | `EXIT_THESIS_FAIL_BARS=3` `EXIT_THESIS_FAIL_MIN_MFE=0.005` | at bar 3, MFE < 0.5% — wrong trade, cut early |
| TrailingStop | `EXIT_TRAILING_ACTIVATION_PCT=0.05` `EXIT_TRAILING_TRAIL_PCT=0.025` | MFE ≥ 5%; trails peak by 2.5% |
| PremiumTarget | `EXIT_PREMIUM_TARGET_PCT=0.99` | effectively disabled — trail handles profits |

## MomentumReversal — what "strategy says exit" means

The lottery stack includes `MomentumReversalPolicy`. It checks `position.current_shadow_score`
on every bar. If the shadow score flips hard against the trade direction (e.g. a PE trade
while score rises above +1.0 = market now bullish), exit immediately with `REGIME_SHIFT`.

This is the only exit that uses live market state to say "the thesis broke" rather than
waiting for a price stop or time gate.

`current_shadow_score` is updated every bar from the composite regime signal. Set
`LOTTERY_MOMENTUM_FLIP=0` to disable this policy (simpler debugging).

## ExpiryAware override (optional)

Near expiry, theta dominates and a stalled trade bleeds fast. Enable a tighter override:

```
EXIT_EXPIRY_OVERRIDE_ENABLED=1
EXIT_EXPIRY_DTE_THRESHOLD=0      # 0 = expiry day only
EXIT_EXPIRY_HARD_STOP_PCT=0.15
EXIT_EXPIRY_THESIS_FAIL_BARS=3
EXIT_EXPIRY_TRAIL_ACTIVATION_PCT=0.03
EXIT_EXPIRY_TRAIL_PCT=0.015
```

## Why legacy inline exits exist (and when they run)

The tracker still carries legacy exits (`_is_thesis_fail_exit`, `_is_stagnant_exit`, etc.)
as a safety net for `EXIT_STRATEGY_MODE=scalper`. The legacy thesis_fail **requires
pnl ≤ −8%** before firing — it almost never cuts dead trades early. This is a known
gap. When `EXIT_POLICY_STACK_ENABLED=1` and mode is `adaptive` or `lottery`, legacy
exits are fully suppressed. Only the stack runs.

## Exit triggers in logs

| trigger value | meaning |
|---|---|
| `exit_stack` | fired by the composite stack (check `exit_reason` for which policy) |
| `forced` | forced_exit_reason (broker, risk manager) |
| `risk_breach` | daily/weekly loss limit hit |
| `hard_close` | 15:20 IST EOD |
| `soft_close` | 15:10 IST EOD |
| `thesis_fail` / `premium_stop` / etc. | legacy inline exits (only if stack disabled) |

Exit reason in the signal: `STOP_LOSS`, `TARGET_HIT`, `TRAILING_STOP`, `REGIME_SHIFT`, `THESIS_FAIL`, `TIME_STOP`, `RISK_BREACH`.
