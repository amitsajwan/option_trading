# Debit options: CE + PE by regime (multi-strategy)

**Policy:** Live and paper trading use **long premium only** (buy CE or buy PE). No short/written options until margin policy changes.

**Profile:** `debit_multi_v1` (`strategy_app/engines/profiles.py`)

---

## How the system is “clever”

The deterministic engine already supports **many strategies per day**. Each snapshot:

1. **Regime** is classified (`TRENDING`, `SIDEWAYS`, `HIGH_VOL`, …).
2. **Router** runs only the strategies enabled for that regime.
3. Each strategy votes **CE**, **PE**, or **AVOID**.
4. The engine picks **one** entry (highest confidence among allowed votes).

So you do **not** need one monolithic strategy. You need the right **playbook per regime**.

| Regime | Strategies active | Typical leg |
|--------|-------------------|-------------|
| `TRENDING` | `IV_FILTER`, `R2_TOP3_LONG_CE` | **Buy CE** on ORB-up / momentum |
| `SIDEWAYS` | `IV_FILTER`, `R1_TOP3_LONG_PE` | **Buy PE** on ORB-down fade |
| `PRE_EXPIRY` | `IV_FILTER`, both top-3 rules | Context-dependent (rare same-bar CE+PE conflict) |
| `HIGH_VOL` / `EXPIRY` | `IV_FILTER` only | Often no new entries |
| `AVOID` | none | Flat |

`IV_FILTER` can veto extreme IV before any debit entry.

---

## Rules vs runtime names

| Runtime strategy | Rule JSON | Direction | Capital model |
|------------------|-----------|-----------|-----------------|
| `R1_TOP3_LONG_PE` | `debit_multi/r1_top3_long_pe_s3.json` | `BUY_ATM_PE` | Pay PE premium |
| `R2_TOP3_LONG_CE` | `debit_multi/r2_top3_long_ce_s3.json` | `BUY_ATM_CE` | Pay CE premium |
| `R1S_TOP3_SHORT_CE` | `r1s_top3/r1s_top3_s3_composite.json` | `SELL_ATM_CE` | **Research only** — needs margin |

R1 long PE uses the **same entry filters** as audited R1S short CE (ORB-down, ret_5m &lt; 0, below VWAP), but **buys PE** instead of selling CE.

---

## Production default vs research vs debit multi

| Profile | Use |
|---------|-----|
| `det_prod_v1` | Legacy multi-strategy (ORB, OI, …) — mixed CE/PE from several playbooks |
| `debit_multi_v1` | **Operator default for capital-constrained debit book** |
| `r1s_top3_paper_v1` | Short-CE research replay / compare to rules_pipeline |

---

## Enable on VM / compose

```env
STRATEGY_PROFILE_ID=debit_multi_v1
```

Rebuild `strategy_app_historical` (and live `strategy_app` when ready). Eval UI: filter by `R1_TOP3_LONG_PE` or `R2_TOP3_LONG_CE`, or leave strategy blank to see both.

---

## Next validation (required before trusting PnL)

1. Rules backtest `BUY_ATM_PE` / `BUY_ATM_CE` top-3 variants on 17Q (same gates as R1S).
2. Compare calm-week PASS rate vs `R1S_SHORT_CE` audit.
3. Replay `debit_multi_v1` May–Jul 2024 and compare trade count / WR to rules baseline.

Until (1–2) pass, treat `debit_multi_v1` as **architecture + paper wiring**, not promoted edge.

### Smoke audit (2026-05-21, ML VM)

Config: `rule_matrix_debit_top3_smoke.json` → `artifacts/rules_runs/debit_top3_smoke_20260521/`

| Rule | may_jul_2024 | aug_oct_2024 |
|------|--------------|--------------|
| **R1S_TOP3_S3** (short CE control) | **PASS** t=+4.02, 49 trades, 81.6% WR | **PASS** t=+3.01, 45 trades |
| R1_TOP3_LONG_PE_S3 | FAIL t=-2.92, 49 trades | FAIL |
| R2_TOP3_LONG_CE_S3 | FAIL t=-2.45, 46 trades | FAIL |
| R1/R2 unlimited buy PE/CE | FAIL (high trade count) | FAIL |

Same entry count (49) for long PE top-3 vs short CE top-3 on May–Jul — **opposite PnL sign** (theta / long vs short premium).

Monthly 153-cell run: `debit_top3_monthly_20260521` (tmux `debit_top3` on ML VM).

---

## CE + PE on the same bar

If both `R1_TOP3_LONG_PE` and `R2_TOP3_LONG_CE` vote on one snapshot, the engine **blocks** unless ML entry policy resolves direction conflict (`det_prod` path). Regime split usually keeps **one-sided** books (trend → CE, sideways → PE). For `PRE_EXPIRY`, both are registered; ORB-up and ORB-down are rarely true together.

---

## Related docs

- [R1S_TOP3_OPERATOR.md](R1S_TOP3_OPERATOR.md) — short-CE research operator sheet
- [R1S_REPLAY_EVAL_INTEGRATION.md](R1S_REPLAY_EVAL_INTEGRATION.md) — replay / eval wiring
