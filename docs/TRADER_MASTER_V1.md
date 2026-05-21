# Trader master profile (`trader_master_v1`)

**Purpose:** Evaluation book for a “very experienced trader” who knows every major playbook in the stack. Not the default for live debit capital — use to **compare breadth vs focused books** (`debit_multi_v1`, `det_core_v2`, etc.).

**Definition:** `strategy_app/engines/profiles.py` → `PROFILE_TRADER_MASTER_V1`

---

## What is armed per regime

| Regime | Entry playbooks (after `IV_FILTER`) |
|--------|-------------------------------------|
| **TRENDING** | ORB, OI_BUILDUP, PREV_DAY_LEVEL, R2_TOP3_LONG_CE, R1S_TOP3_SHORT_CE, TRADER_COMPOSITE, TRADER_V3_COMPOSITE, PBV1_TOP3_THESIS |
| **SIDEWAYS** | VWAP_RECLAIM, OI_BUILDUP, R1_TOP3_LONG_PE, R1S_TOP3_SHORT_CE, TRADER_COMPOSITE, TRADER_V3_COMPOSITE, PBV1_TOP3_THESIS |
| **PRE_EXPIRY** | Union of trending + sideways toolkit |
| **EXPIRY** | VWAP_RECLAIM, TRADER_V3_COMPOSITE |
| **HIGH_VOL** | HIGH_VOL_ORB, TRADER_V3_COMPOSITE, R1S_TOP3_SHORT_CE |
| **AVOID** | None |

**Exit helpers** (manage open positions; not regime entry list): ORB, OI_BUILDUP, HIGH_VOL_ORB, VWAP_RECLAIM, PREV_DAY_LEVEL, debit top-3, composites, R1S, PBV1.

**Risk:** stop 25%, target 70%, trailing on (activation 12%, offset 6%, lock BE).

**Engine:** Same deterministic router + brain gates as other profiles. One winning vote per bar among eligible strategies.

---

## Enable on VM

```bash
bash ops/gcp/patch_trader_master_env.sh /opt/option_trading/.env.compose
cd /opt/option_trading
sudo docker compose build strategy_app_historical dashboard
sudo docker compose up -d --force-recreate strategy_app_historical dashboard
```

Preflight before replay:

```bash
sudo python3 ops/gcp/preflight_historical_replay.py
```

---

## Evaluate (Eval UI)

1. Confirm banner: **VM replay: Trader master (full book)**.
2. Date range e.g. **May–Jul 2024** or **Oct 2024** preset → **Run Replay**.
3. Leave Strategy = **All in scope** (profile entries only).
4. Compare **Strategy performance** / **Regime performance** vs a `debit_multi_v1` run on the same window.

Switch back to debit:

```bash
bash ops/gcp/patch_debit_multi_env.sh /opt/option_trading/.env.compose
sudo docker compose up -d --force-recreate strategy_app_historical
```

---

## Expectations

- **More trades** than `debit_multi_v1` (many voters per regime).
- **Mixed directions** (long CE/PE + short CE R1S + ORB/OI legacy paths).
- Use **Option PnL%** for comparison; capital return on $1k notional is misleading on multi-leg books.
- R1S short CE in the book is for **research parity**; live debit policy may still prefer `debit_multi_v1` only.
