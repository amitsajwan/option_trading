# 05 — Config Reference

*Every operator-facing env var, grouped, with default and meaning. This is the
config-driven contract: behaviour changes here, not in code. Set in
`.env.compose` on the VM (live) or via OPS-sim overrides (experiments).*

> **Now consolidated** → every tunable below is declared once in
> `strategy_app/config/registry.py`, lives in `ops/strategy_config.yml`, and is
> read by BOTH live and sim via `strategy_app/config/loader.py` — SIM can no
> longer diverge from LIVE. The always-fresh, auto-generated table is
> [`CONFIG_REGISTRY_TABLE.md`](CONFIG_REGISTRY_TABLE.md) (run
> `python ops/gen_config_docs.py`). The prose below is curated context/findings;
> the generated table is the authoritative key list. Plan + cutover steps:
> [`CONFIG_CONSOLIDATION_PLAN.md`](CONFIG_CONSOLIDATION_PLAN.md).

> Source of truth at runtime (today): `strategy_app/main.py` writes the live values it actually
> used to `.run/strategy_app/ops_env.json`. The OPS sim reads that file as its baseline.

---

## Strategy mode (the big switch)

| Var | Default | Meaning |
|---|---|---|
| `EXIT_STRATEGY_MODE` | `scalper` | `scalper` = capture small gains, tight exits, inline backstops active. `lottery` = let winners run, stack is sole authority. |
| `EXIT_POLICY_STACK_ENABLED` | `0` | Master switch for the composite exit stack. Must be `1` for either mode's stack to run. |

---

## Scalper exit stack (`EXIT_STRATEGY_MODE=scalper`)

| Var | Default | Meaning |
|---|---|---|
| `EXIT_PREMIUM_TARGET_PCT` | `0.04` | Emergency profit target (4%). Set high so it doesn't pre-empt the trail. |
| `EXIT_TRAILING_ACTIVATION_PCT` | `0.01` | Trailing stop activates once MFE ≥ 1%. |
| `EXIT_TRAILING_TRAIL_PCT` | `0.005` | Once active, exit if price gives back 0.5% from peak. |
| `EXIT_THESIS_FAIL_BARS` | `3` | Cut a trade that hasn't moved after N bars. |
| `EXIT_THESIS_FAIL_MIN_MFE` | `0.002` | "Hasn't moved" = MFE < 0.2%. |

Scalper keeps the tracker's **inline** exits (stop-loss, max_hold, etc.) as backstops —
the scalper stack has no hard stop of its own.

---

## Lottery exit stack (`EXIT_STRATEGY_MODE=lottery`)

| Var | Default | Meaning |
|---|---|---|
| `LOTTERY_HARD_STOP_PCT` | `0.20` | Cap the loss at −20% (the "ticket price"). Set ≥ `1.0` to disable (ride to zero). |
| `LOTTERY_BIG_TARGET_PCT` | `0.50` | Take the lottery win at +50%. |
| `LOTTERY_RUNNER_ACTIVATION_MFE` | `0.20` | Runner trail activates only after MFE ≥ 20%. |
| `LOTTERY_RUNNER_GIVEBACK_FRAC` | `0.35` | Once active, exit if pnl falls below peak×(1−0.35). Loose — lets 20%→50% run. |
| `LOTTERY_THESIS_FAIL_BARS` | `999` | **Disabled by design** — lottery tickets are NOT cut on thesis-fail; the timestop is the backstop. (A finite value here would defeat letting winners run.) |
| `LOTTERY_THESIS_FAIL_MIN_MFE` | `0.03` | Only relevant if `THESIS_FAIL_BARS` < 999: don't kill a ticket that showed ≥3% promise. |
| `LOTTERY_MOMENTUM_FLIP` | `1.0` | Exit if shadow score flips against the thesis by this magnitude. `0` = off. |
| `LOTTERY_TIMESTOP_BARS` | `90` | Max hold (90 min) — the **real** backstop for lottery tickets since thesis-fail is off. |

**Recommended protected lottery config** (from the sweep): `MOMENTUM_FLIP=0`,
`RUNNER_ACTIVATION_MFE=0.10`, `RUNNER_GIVEBACK_FRAC=0.35`, `TIMESTOP_BARS=90`.
In lottery mode the stack is authoritative — inline exits are suppressed.

---

## Entry gate

| Var | Default | Meaning |
|---|---|---|
| `CONSENSUS_BYPASS_MIN_CONFIDENCE` | `0.65` | Min ML entry confidence for the consensus-bypass path. Lottery uses `0.80` (rare, high conviction). |
| `DIRECTION_MIN_MARGIN_SIDEWAYS` | `2.0` | Higher direction margin required in SIDEWAYS regime (noisy). Global default margin is 1.25. |
| `STRATEGY_MIN_CONFIDENCE` | `0.50` | Base engine min confidence (non-consensus paths). |
| `ENTRY_ML_MIN_PROB` | `0.40` | Min probability from `entry_only_v3` to allow ML_ENTRY. Recommended: 0.40 (model's optimal). Do NOT set above 0.85 — that excludes the dominant 0.826 bucket and silences the model. |
| `REGIME_ALLOWED` | `MID,TREND` | **Direction quality gate** (RegimeDirector TREND/MID/CHOP — NOT the Regime enum). Bars where quality=CHOP are blocked. See `docs/TWO_REGIME_SYSTEMS.md`. |

---

## Vol-gate entry (`ENTRY_VOL_GATE_ENABLED=1`)

Swap-in alternative to ML_ENTRY. Trigger = ATR-based volatility gate; direction logic is shared.

| Var | Default | Meaning |
|---|---|---|
| `ENTRY_VOL_GATE_ENABLED` | `0` | `1` = activate VolGateEntry (replaces ML_ENTRY). `0` = use ML_ENTRY. |
| `ATR_ENTRY_MIN_PCT` | `0.00088` | Gate: `atr_14_1m / price >= this`. Default 0.00088 ≈ p90 of live ATR (≈3× lift over base move rate). For 2024 backtests use 0.0006 (lower vol). **0.0006 is a knife-edge in backtests** — do not treat as validated for live. |
| `ATR_ENTRY_MIN_ABS` | `0` | If >0, gate on absolute `atr_14_1m >= this` instead of pct. Escape hatch; prefer pct. |
| `ATR_ENTRY_BB_MIN` | `0` | Optional Bollinger-band width confirm (`bb_width_5m >= this`). Default 0 = off. |

---

## Direction detector (`REGIME_DIRECTION_SIGNAL`)

| Var | Default | Meaning |
|---|---|---|
| `REGIME_DIRECTION_SIGNAL` | `weighted` | `weighted` = graceful (absent member → 0 weight). `combo` / `agreement_lever` = hard-block (ALL trio members must agree; absent = ABSTAIN). **Use `weighted`** — in 2024, OI/max_pain absent ~46% of bars, so combo/agreement_lever ABSTAIN always. |
| `REGIME_W_MOM` | `1.0` | Weight for `momentum_15m`. **Set to 0** — it is an ANTI-signal (48.1% accuracy, confirmed 2026-06-14 over 37,050 bars in both halves). |
| `REGIME_W_VWAP` | `1.0` | Weight for VWAP. Mildly anti (50.5% acc) — consider reducing to 0.5. |
| `REGIME_W_MAXPAIN` | `0.8` | Weight for max pain (51.2% acc, H2 52.8%). Positive contributor — keep. |
| `REGIME_W_OI` | `0.8` | Weight for ATM OI signal (52.1% acc, best individual). Keep. |
| `REGIME_W_EMA` | `0.5` | Weight for EMA trend signal. Modest positive — keep. |

---

## Strike selection

| Var | Default | Meaning |
|---|---|---|
| `STRATEGY_STRIKE_SELECTION_POLICY` | `atm` | `atm` = always ATM. `smart_strike` = tiered OTM selection. |
| `STRATEGY_SMART_STRIKE_ENABLED` | `1` | Master switch for smart-strike tiers. |
| `SMART_STRIKE_MAX_PREMIUM` | `600` | Budget target (₹). Selector tries to stay under this; falls back to ATM if nothing fits. |
| `STRATEGY_STRIKE_MAX_OTM_STEPS` | `0` | **Currently ignored by the tier builder (bug, STRIKE-S1).** Intended max OTM depth. |
| `SMART_STRIKE_IV_REJECT_PCTILE` | `90` | Skip the trade entirely if IV percentile above this. |
| `SMART_STRIKE_OTM_IV_CEIL` | `92` | **Percentile** ceiling for OTM-1 (was 60 — the absolute-vs-percentile bug, Findings §3.3). |
| `SMART_STRIKE_OTM2_IV_CEIL` | `91` | Percentile ceiling, OTM-2. |
| `SMART_STRIKE_OTM3_IV_CEIL` | `90` | Percentile ceiling, OTM-3. |
| `SMART_STRIKE_OTM4_IV_CEIL` | `89` | Percentile ceiling, OTM-4. |
| `SMART_STRIKE_OTM{2,3,4}_ENABLED` | `1` | Enable each deeper tier. |
| `SMART_STRIKE_OTM{,2,3,4}_CONFIDENCE` | `0.55/0.65/0.75/0.85` | Min confidence per tier (deeper needs more). |
| `SMART_STRIKE_OTM{3,4}_REGIMES` | `BREAKOUT,TRENDING / BREAKOUT` | Regime restriction for deep tiers. |
| `SMART_STRIKE_OTM{2,3,4}_MIN_OI` | `100k/75k/50k` | Liquidity floor per tier. |

> Live currently pins the **old** absolute-style IV ceilings (60/50/40/30) via env, so
> live stays ATM on high-IV days. The corrected percentile ceilings are the code
> defaults and the OPS-sim baseline. Promote by updating `.env.compose`.

---

## Risk & sizing

| Var | Default | Meaning |
|---|---|---|
| `RISK_MAX_SESSION_TRADES` | `6` | Max trades/day. Lottery preset uses `3`. |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` | Halt after N consecutive losses. |
| `RISK_MAX_LOTS_PER_TRADE` | `5` | Hard lot cap. |
| `RISK_CAPITAL_ALLOCATED` | `500000` | Capital base for risk sizing. |
| `RISK_PER_TRADE_PCT` | `0.005` | Fraction of capital risked per trade (risk-based sizing). |
| `RISK_CALCULATOR` | `fixed_fraction` | Sizing model. `FixedFractionRisk`: lots = floor(capital×pct / (premium×lot×stop)). |
| `RISK_FRACTION_PCT` | `0.01` | Fraction at risk for `fixed_fraction`. |
| `TRANSACTION_COST_PER_LOT` | `50` | ₹/lot (brokerage+STT+charges) deducted in `avg_net_pnl_pct`. |

---

## Execution

| Var | Default | Meaning |
|---|---|---|
| `EXECUTION_ADAPTER` | `paper` | `paper` (no real orders) / `kite` (real NFO) / `shadow` (real 1 lot + paper). |
| `KITE_API_KEY`, `KITE_ACCESS_TOKEN` | — | Required for kite/shadow. Token auto-refreshed by `ingestion_app.token_refresh`. |
| `SHADOW_MAX_LOTS` | `1` | Caps real Kite orders in shadow mode. |
| `ORDER_FILL_TIMEOUT_SEC` | `30` | Give up waiting for a fill after this. |
| `FILL_TRACKER_ENABLED` | `1` | Consume `execution:fills:v1` → MongoDB real P&L. |

---

## Alerts & ops

| Var | Default | Meaning |
|---|---|---|
| `ALERT_ENABLED` | `0` | Telegram alerts on open/close/halt/reject. |
| `ALERT_TELEGRAM_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` | — | Telegram bot creds. |
| `STRATEGY_PROFILE_ID` | `trader_master_ml_entry_consensus_v1` | Active strategy profile (router + risk config). |
| `STRATEGY_RUN_DIR` | `/app/.run/strategy_app` | JSONL output dir. **Sims must override to `/tmp/...`.** |
| `STRATEGY_REDIS_PUBLISH_ENABLED` | `1` | **Sims must set `0`** (don't publish to live topics). |

---

## OPS-sim-only override keys

The OPS tool validates overrides against an allow-list (`ops_routes._SAFE_OVERRIDE_KEYS`).
Currently includes all `EXIT_*`, `LOTTERY_*`, `CONSENSUS_BYPASS_MIN_CONFIDENCE`,
`DIRECTION_MIN_MARGIN_SIDEWAYS`, strike vars, `RISK_MAX_*`, `STRATEGY_PROFILE_ID`,
`EXIT_STRATEGY_MODE`. Add new tunables here when you add them so they're sim-testable.

---

## How a config flows (live vs sim)

**Today (being replaced):**
```
LIVE:  .env.compose ─► docker-compose env ─► strategy_app process env
                                          └─► main.py writes ops_env.json (the truth)

SIM:   ops_env.json (live baseline)  +  operator overrides (OPS drawer / API)
       ─► applied to os.environ under a lock ─► fresh engine ─► /tmp run dir
```
The SIM path re-defaults any var missing from `ops_env.json` using its *own*
hardcoded fallbacks — this is the divergence source (see consolidation plan §1).

**Target (after consolidation):**
```
ops/strategy_config.yml ─► load_config() ─► os.environ ─┬─► LIVE (main.py)
                           (one loader, one registry)   └─► SIM (+ operator overrides)
```
Both paths call the **same loader on the same file**. SIM divergence becomes
structurally impossible.

**Golden rule:** to change behaviour, edit `ops/strategy_config.yml` (today:
`.env.compose`) and restart. To *test* a change, pass it as an OPS-sim override
first. Never edit code to change a number.
