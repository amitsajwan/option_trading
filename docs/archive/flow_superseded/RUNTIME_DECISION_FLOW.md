# Runtime Decision Flow

> **As-of:** 2026-05-19 · **Owner:** strategy_app / pure_ml_engine
>
> What happens between a market snapshot arriving and a trade being placed
> (or not). Every gate is enumerated with file:line refs and the JSONL it
> writes when it fires.

For a higher-level architecture view, see [`SYSTEM_FLOW_DIAGRAMS.md`](SYSTEM_FLOW_DIAGRAMS.md).
For what the model itself emits, see [`MODEL_OUTPUT_CONTRACT.md`](MODEL_OUTPUT_CONTRACT.md).
For where to look when something doesn't fire, see [`OBSERVABILITY_GUIDE.md`](OBSERVABILITY_GUIDE.md).

---

## TL;DR

A snapshot arrives every minute. The engine evaluates a chain of gates.
**Any gate failing causes HOLD; only when all gates pass does a trade fire.**
The model itself is just one gate in the chain.

```
snapshot -> [pre-fire gates] -> model -> [threshold] -> [liquidity] -> ENTRY
                                              |             |
                                              |             v
                                              |          HOLD (with reason)
                                              v
                                            HOLD (model says low confidence)
```

Once a position is open, **exit gates** are evaluated every subsequent
snapshot until one of them closes the position. The model has no say
in when to exit — recipe parameters + risk manager rules decide.

---

## Pre-fire gate chain (in `PureMLEngine.evaluate`)

All file:line refs are in [`strategy_app/engines/pure_ml_engine.py`](../strategy_app/engines/pure_ml_engine.py).

| Order | Gate | Where | Fires when | HOLD reason emitted |
|---:|---|---|---|---|
| 1 | **Single-position lock** | `_tracker.has_position` (called before evaluate) | Already holding a position | `position_already_open` (handled outside evaluate) |
| 2 | **Post-stop cooldown** | `evaluate:395-397` | A previous trade STOP_LOSS'd within last N bars (`ML_PURE_POST_STOP_COOLDOWN_BARS`) | `post_stop_cooldown` |
| 3 | **RISK_BREACH cooldown** | `evaluate:403-405` | A previous trade exited with RISK_BREACH within last 5 bars (`OPTION_PNL_RISK_BREACH_COOLDOWN_BARS`) | `risk_breach_cooldown` |
| 4 | **Daily soft halt** | `evaluate:411-412` | Cumulative day P&L < `OPTION_PNL_DAILY_SOFT_HALT_PCT` (default −0.20) | `daily_soft_halt` |
| 5 | **Soft close** | `evaluate:419-421` | Snapshot time ≥ 15:00 IST (no new entries late session) | `soft_close_no_entry` |
| 6 | **Model evaluation** | `evaluate:428-437` | Always runs unless 1-5 failed. Calls `select_best_bundle_decision` (multi-bundle path) or `predict_staged` (3-stage path) | — |
| 7 | **Threshold gate (model)** | `evaluate:444-446` | `decision.action == "HOLD"` (model's own `entry_prob < threshold`) | reason from `decision.reason` (e.g. `entry_below_threshold`, `direction_below_threshold`) |
| 8 | **Bundle ATM-strike check** | `evaluate:498-502` | option-PnL bundle predictor returned no `selected_strike` | `missing_atm_or_strike_step_for_bundle` |
| 9 | **Premium availability** | `evaluate:525-527` | Selected strike has no premium quote | `missing_option_premium` |
| 10 | **Liquidity gate** | `evaluate:_liquidity_ok` | Chosen strike OI < `ML_PURE_MIN_OI` (50k) OR Volume < `ML_PURE_MIN_VOLUME` (15k). Skipped for bundle-preselected strikes. | `liquidity_gate_block` (set by `_liquidity_ok`) |
| 11 | **Confidence floor** | risk manager pre-check | `signal.confidence < min_confidence` (default 0.65). Belt-and-braces below the model threshold. | filtered upstream |

Once gates 1-11 all pass, the engine emits a `TradeSignal(signal_type=ENTRY)`
via `log_signal(...)`. The signal then goes through:

- `_annotate_signal_contract` — attaches recipe parameters, exit rules
- Risk manager final adjustments (lot count, position size multiplier)
- Broker submission (or paper-fill engine if `STRATEGY_ROLLOUT_STAGE=paper`)

---

## Exit gate chain (continuous while position open)

While `_tracker.has_position`, every new snapshot triggers `evaluate` which
short-circuits at line ~248 into the position-management branch instead of
new-entry. Exit reasons (from [`strategy_app/contracts.py:ExitReason`](../strategy_app/contracts.py)):

| Exit reason | Trigger | Where |
|---|---|---|
| `STOP_LOSS` | Premium drops by `stop_pct_of_premium` from entry (default 25%) | `position/tracker.py` |
| `TARGET_HIT` | Premium rises by `target_pct_of_premium` from entry (default 40%) | `position/tracker.py` |
| `TIME_STOP` | `bars_held >= max_hold_bars` (recipe-driven; 9 or 15 typically) | `position/tracker.py` |
| `TRAILING_STOP` | Dynamic trail: arms when MFE > `trailing_activation_pct`, exits when price retraces by `trailing_offset_pct` from peak | `position/tracker.py` |
| `RISK_BREACH` | Underlying futures moves > `underlying_stop_pct` against position (default 0.002 = 0.20%), OR daily DD trigger | `risk/manager.py` |

When any exit fires, the engine writes a CLOSE event to `positions.jsonl`,
a CLOSE signal to `signals.jsonl`, and updates `_day_pnl_pct` for the
soft-halt gate (`_handle_position_closed` at `pure_ml_engine.py:1079-1101`).

---

## Risk manager safety nets

These sit alongside (and outside) the per-snapshot evaluate loop. They can
force-close an open position or halt the engine entirely.

| Net | Config | What it does |
|---|---|---|
| **Daily DD hard halt** | `halt_daily_dd_pct` (default −0.75) | Force-close + halt new entries for the day when day P&L crosses −75% |
| **Consecutive loss halt** | `halt_consecutive_losses` (default 3) | Pause new entries after N losing trades in a row |
| **Position size multiplier** | `ML_PURE_SIZE_MULTIPLIER` | Scales recipe lot count (0.25× default = paper stage) |
| **Rollout stage** | `STRATEGY_ROLLOUT_STAGE` (`paper` / `capped_live` / `live`) | `paper` simulates fills; only `live` sends real orders |
| **Block expiry days** | `STRATEGY_BLOCK_EXPIRY` | Refuse all new entries on Wednesday weekly expiry |

The daily SOFT halt (−20%) is the option-PnL-specific layer on top of these
hard halts. The soft halt **only blocks new entries**; existing positions
continue to manage until their own exit gates fire.

---

## Date-boundary resets

`PureMLEngine.on_session_start(trade_date)` runs at the start of each
trading day. It resets:

- `_cooldown_bars_remaining = 0`
- `_option_pnl_risk_breach_cooldown_remaining = 0`
- `_day_pnl_pct = 0.0`
- `_day_halt_active = False`
- `_hold_counts = {}` (aggregate counter)
- Feature-state + position-tracker + risk-manager have their own `on_session_start` hooks

So all per-day gates start fresh each day; cooldowns and halts do not
carry across overnight.

---

## What this means for "why didn't this trade fire?"

For any given minute that did NOT produce an entry, the diagnostic path is:

1. Grep `signals.jsonl` for `snapshot_id == <minute>` — if a HOLD signal
   exists, the `reason` field tells you which gate blocked. Example
   `ml_pure_hold:risk_breach_cooldown`.
2. If no HOLD signal at all for that minute, the engine never reached
   evaluate for that snapshot — check `metrics.jsonl` for a `session_start`
   anomaly or a snapshot drop earlier in the pipeline.
3. To trace the chain inside the engine, set `STRATEGY_ML_PURE_BYPASS_GATES=1`
   in dev: this bypasses gates 2-5 and 8-11 to see what the model alone
   would have decided. Never set in production.

The future `decisions.jsonl` (planned in this session's instrumentation
work) will collapse this into a single line per snapshot listing all gates
evaluated and which (if any) blocked — making the question answerable from
one grep instead of three files.

---

## Configuration summary

All gates are env-var-configurable from `docker-compose.yml`. Defaults are
in `pure_ml_engine.py:__init__` and the risk manager.

| Env var | Default | Gate |
|---|---|---|
| `ML_PURE_POST_STOP_COOLDOWN_BARS` | `0` | Post-stop cooldown |
| `OPTION_PNL_RISK_BREACH_COOLDOWN_BARS` | `5` | RISK_BREACH cooldown |
| `OPTION_PNL_DAILY_SOFT_HALT_PCT` | `-0.20` | Daily soft halt |
| `ML_PURE_MIN_OI` | `50000` | Liquidity gate |
| `ML_PURE_MIN_VOLUME` | `15000` | Liquidity gate |
| `ML_PURE_UNDERLYING_STOP_PCT` | `0.002` | RISK_BREACH exit (futures move) |
| `ML_PURE_UNDERLYING_TARGET_PCT` | `0.005` | Reserved (not currently used for exits) |
| `ML_PURE_MAX_HOLD_BARS` | `15` | Default time stop (recipe overrides) |
| `STRATEGY_BLOCK_EXPIRY` | `0` | Block expiry days |
| `STRATEGY_ROLLOUT_STAGE` | `paper` | Rollout safety |
| `STRATEGY_ML_PURE_BYPASS_GATES` | `0` | Dev-only: bypass gates 2-5,8-11 |

The model's own threshold (e.g. 0.55) lives in the bundle's `metadata.json`,
not in env. To swap thresholds you patch the bundle or change which bundle
is loaded via `OPTION_PNL_MODEL_BUNDLE`.
