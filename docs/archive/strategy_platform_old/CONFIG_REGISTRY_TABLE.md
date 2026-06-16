# Config Registry Table (generated)

> **Do not edit by hand.** Generated from `strategy_app/config/registry.py`
> by `ops/gen_config_docs.py`. Values live in `ops/strategy_config.yml`;
> both live and sim read them via `strategy_app/config/loader.py`.
> See `CONFIG_CONSOLIDATION_PLAN.md`.

Total keys: **60** (43 sim-overridable).

## execution

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `execution.adapter` | `EXECUTION_ADAPTER` | str | `dhan` |  | paper \| dhan \| kite \| shadow. Read by execution_app (not the loader yet); SIM forces read-only behaviour regardless. |
| `execution.rollout_stage` | `rollout_stage` | str | `paper` |  | paper \| live. Cosmetic — real orders gate on grade, not this. |

## profile

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `profile.id` | `STRATEGY_PROFILE_ID` | str | `trader_master_live_v1` | ✓ | Active strategy profile (router + risk config). |

## entry

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `entry.time_windows` | `ENTRY_TIME_WINDOWS` | str | `09:45-14:30` | ✓ | IST entry window(s). |
| `entry.min_confidence` | `STRATEGY_MIN_CONFIDENCE` | float | `0.65` | ✓ | Base engine min confidence (non-consensus paths). |
| `entry.bypass_min_confidence` | `CONSENSUS_BYPASS_MIN_CONFIDENCE` | float | `0.65` | ✓ | Min ML entry confidence for consensus-bypass path. |
| `entry.pipeline_v2` | `STRATEGY_ENTRY_PIPELINE_V2` | bool | `0` | ✓ | v2 gate-cascade entry pipeline. Off = legacy paths. |
| `entry.vol_gate.enabled` | `ENTRY_VOL_GATE_ENABLED` | bool | `1` | ✓ | 1 = ATR vol gate trigger; 0 = entry_only_v3 ML model. |
| `entry.vol_gate.atr_min_pct` | `ATR_ENTRY_MIN_PCT` | float | `0.00088` | ✓ | ATR gate: atr_14_1m / price >= this (~p90 live). |
| `entry.ml.model_path` | `ENTRY_ML_MODEL_PATH` | str | `/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib` | ✓ | Entry ML bundle path (live = 020pct). |
| `entry.ml.min_prob` | `ENTRY_ML_MIN_PROB` | float | `0.45` | ✓ | Min entry_only_v3 prob (only used when vol_gate.enabled=0). |

## direction

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `direction.mode` | `ML_ENTRY_DIRECTION_MODE` | str | `regime_dual` | ✓ | composite \| regime_dual \| consensus. Live = regime_dual. |
| `direction.ml.model_path` | `DIRECTION_ML_MODEL_PATH` | str | `/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib` | ✓ | Direction ML bundle path. |
| `direction.ml.weight` | `DIRECTION_ML_WEIGHT` | float | `0.4` | ✓ | ML tilt weight in regime_dual (40% ML / 60% composite). |
| `direction.ml.filter_min_prob` | `DIRECTION_ML_FILTER_MIN_PROB` | str | `*(empty)*` | ✓ | Optional hard ML prob filter. Blank = off. |
| `direction.min_margin_sideways` | `DIRECTION_MIN_MARGIN_SIDEWAYS` | float | `2.0` | ✓ | Higher direction margin required in SIDEWAYS regime. |

## regime

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `regime.trend_score_min` | `REGIME_TREND_SCORE_MIN` | float | `2.0` | ✓ | Score needed to classify TRENDING (vs SIDEWAYS). |
| `regime.aligned_bonus` | `REGIME_TREND_ALIGNED_BONUS` | float | `0.0` | ✓ | Bonus when 5/15/30m returns align. 0 = off. |
| `regime.vol_ratio_min` | `REGIME_TREND_VOL_RATIO_MIN` | float | `1.3` | ✓ | vol_ratio threshold for strong_vol contribution. |

## exit

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `exit.mode` | `EXIT_STRATEGY_MODE` | str | `adaptive` | ✓ | scalper \| adaptive (TRENDING/BREAKOUT->lottery, rest->scalper). |
| `exit.max_loss_pct` | `EXIT_MAX_LOSS_PCT` | float | `0.1` | ✓ | Universal loss floor wrapping every mode. |
| `exit.policy_stack_enabled` | `EXIT_POLICY_STACK_ENABLED` | bool | `1` | ✓ | Master switch for the composite exit stack. |
| `exit.scalper.hard_stop_pct` | `EXIT_SCALPER_HARD_STOP_PCT` | float | `0.07` | ✓ | Scalper hard stop (% of entry). |
| `exit.scalper.target_pct` | `EXIT_PREMIUM_TARGET_PCT` | float | `0.03` | ✓ | Scalper profit target (% of entry). |
| `exit.scalper.trailing_activation_pct` | `EXIT_TRAILING_ACTIVATION_PCT` | float | `0.015` | ✓ | Trailing stop activates once MFE >= this. |
| `exit.scalper.trailing_trail_pct` | `EXIT_TRAILING_TRAIL_PCT` | float | `0.008` | ✓ | Once active, exit on giveback of this from peak. |
| `exit.scalper.thesis_fail_bars` | `EXIT_THESIS_FAIL_BARS` | int | `999` | ✓ | Cut flat scalper trade after N bars. 999 = disabled. |
| `exit.scalper.thesis_fail_min_mfe` | `EXIT_THESIS_FAIL_MIN_MFE` | float | `0.002` | ✓ | 'Hasn't moved' = MFE < this. |
| `exit.lottery.regimes` | `ADAPTIVE_LOTTERY_REGIMES` | csv | `BREAKOUT,TRENDING` | ✓ | Regimes routed to the lottery stack in adaptive mode. |
| `exit.lottery.hard_stop_pct` | `LOTTERY_HARD_STOP_PCT` | float | `0.2` | ✓ | Lottery loss cap (universal floor may fire first). |
| `exit.lottery.big_target_pct` | `LOTTERY_BIG_TARGET_PCT` | float | `0.5` | ✓ | Lottery profit target. |
| `exit.lottery.runner_activation_mfe` | `LOTTERY_RUNNER_ACTIVATION_MFE` | float | `0.2` | ✓ | Runner trail activates after MFE >= this. |
| `exit.lottery.runner_giveback_frac` | `LOTTERY_RUNNER_GIVEBACK_FRAC` | float | `0.35` | ✓ | Once active, exit if pnl < peak*(1-this). |
| `exit.lottery.thesis_fail_bars` | `LOTTERY_THESIS_FAIL_BARS` | int | `999` | ✓ | 999 = disabled; timestop is the lottery backstop. |
| `exit.lottery.thesis_fail_min_mfe` | `LOTTERY_THESIS_FAIL_MIN_MFE` | float | `0.03` | ✓ | Only relevant when thesis_fail_bars < 999. |
| `exit.lottery.timestop_bars` | `LOTTERY_TIMESTOP_BARS` | int | `90` | ✓ | Max hold (min) — real lottery backstop. |
| `exit.lottery.momentum_flip` | `LOTTERY_MOMENTUM_FLIP` | float | `1.0` | ✓ | Exit if shadow score flips by this magnitude. 0 = off. |

## strike

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `strike.policy` | `STRATEGY_STRIKE_SELECTION_POLICY` | str | `otm` | ✓ | atm \| otm \| smart_strike. |
| `strike.smart_strike_enabled` | `STRATEGY_SMART_STRIKE_ENABLED` | bool | `1` | ✓ | Master switch for smart-strike tiers. |
| `strike.min_premium` | `SMART_STRIKE_MIN_PREMIUM` | int | `0` | ✓ | Premium floor (0 = no floor; matches live code default). |
| `strike.max_premium` | `SMART_STRIKE_MAX_PREMIUM` | int | `1300` | ✓ | Don't buy more expensive than this. |
| `strike.max_otm_steps` | `STRATEGY_STRIKE_MAX_OTM_STEPS` | int | `12` | ✓ | Max OTM depth in steps. |
| `strike.otm.confidence` | `SMART_STRIKE_OTM_CONFIDENCE` | float | `0.55` |  | Min confidence for OTM-1 tier. |
| `strike.otm2.enabled` | `SMART_STRIKE_OTM2_ENABLED` | bool | `1` |  | Enable OTM-2. |
| `strike.otm2.confidence` | `SMART_STRIKE_OTM2_CONFIDENCE` | float | `0.65` |  | Min conf OTM-2. |
| `strike.otm3.enabled` | `SMART_STRIKE_OTM3_ENABLED` | bool | `1` |  | Enable OTM-3. |
| `strike.otm3.confidence` | `SMART_STRIKE_OTM3_CONFIDENCE` | float | `0.75` |  | Min conf OTM-3. |
| `strike.otm3.regimes` | `SMART_STRIKE_OTM3_REGIMES` | csv | `BREAKOUT,TRENDING` |  | Regime restriction for OTM-3. |
| `strike.otm4.enabled` | `SMART_STRIKE_OTM4_ENABLED` | bool | `1` |  | Enable OTM-4. |
| `strike.otm4.confidence` | `SMART_STRIKE_OTM4_CONFIDENCE` | float | `0.85` |  | Min conf OTM-4. |
| `strike.otm4.regimes` | `SMART_STRIKE_OTM4_REGIMES` | csv | `BREAKOUT` |  | Regime restriction for OTM-4. |
| `strike.otm2.min_oi` | `SMART_STRIKE_OTM2_MIN_OI` | int | `75000` |  | Liquidity floor OTM-2. |
| `strike.otm3.min_oi` | `SMART_STRIKE_OTM3_MIN_OI` | int | `75000` |  | Liquidity floor OTM-3. |
| `strike.otm4.min_oi` | `SMART_STRIKE_OTM4_MIN_OI` | int | `50000` |  | Liquidity floor OTM-4. |

## risk

| YAML path | Env var | Type | Default | Sim? | Meaning |
|---|---|---|---|:--:|---|
| `risk.max_consecutive_losses` | `RISK_MAX_CONSECUTIVE_LOSSES` | int | `6` | ✓ | Halt after N consecutive losses. |
| `risk.max_session_trades` | `RISK_MAX_SESSION_TRADES` | int | `6` | ✓ | Max trades/day. |
| `risk.max_lots_per_trade` | `RISK_MAX_LOTS_PER_TRADE` | int | `1` |  | Hard lot cap (live = 1 lot). |
| `risk.capital` | `RISK_CAPITAL_ALLOCATED` | int | `41000` |  | Capital base for sizing (live Dhan balance). |
| `risk.per_trade_pct` | `RISK_PER_TRADE_PCT` | float | `0.005` |  | Fraction risked per trade. |
| `risk.live_min_grade` | `RISK_LIVE_MIN_GRADE` | str | `OK` | ✓ | Grade floor for live eligibility (GOOD \| OK). |

