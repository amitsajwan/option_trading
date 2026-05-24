# Trader v1 playbook (few trades, explicit risk)

**Operator intent:** On a **good** day, take up to **5** ranked setups; on a **bad / unclear** day, **zero** (no signal rows → no trades). Tighter risk than research R1S: **~10% premium stop**, **~30% target**, **trail** after +15% MFE.

**Important:** Rules backtest still does **one full exit** per trade (no scale-out at 50% yet). Live `strategy_app` trailing is separate from rules JSON.

---

## Monthly debit audit recap (2026-05-21)

| Rule | PASS months (of 51) |
|------|---------------------|
| R1S_TOP3 short CE (research exits) | **7** |
| R1_TOP3 long PE | **0** |
| R2_TOP3 long CE | **0** |

Same 7 calm months as prior R1S monthly sweep. **Do not** promote long PE/CE off this matrix.

---

## Trader v1 rule variants (short CE, same ORB-down entry)

| Rule | Max trades/day | Stop | Target | Trail | Hold |
|------|----------------|------|--------|-------|------|
| `R1S_TOP3_S3_COMPOSITE` (baseline) | 3 | 100% | 50% credit | off | 20m |
| `R1S_TOP5_TRADER_EXITS` | **5** | 10% | 30% | 15% act / 8% giveback | 45m |
| `R1S_TOP1_TRADER_EXITS` | **1** | 10% | 30% | same | 45m |

**0 trades/day** happens automatically when entry conditions never fire (no ORB-down fade).

**Good week gate** (skip entire weeks) stays **manual** until a regime rule beats 6/17 PASS — not in these JSON files.

---

## Run smoke on ML VM

```bash
cd /opt/option_trading
.venv/bin/python3 -m ml_pipeline_2.scripts.rules_pipeline.pipeline \
  --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix_trader_v1_smoke.json \
  --output-root ml_pipeline_2/artifacts/rules_runs/trader_v1_smoke_$(date +%Y%m%d)
```

If smoke beats or matches R1S on May–Jul / Aug–Oct, run a monthly matrix (add rules to `build_trader_v1_monthly_matrix.py` when created).

---

## Next wiring (after rules PASS)

1. `strategy_app` profile with `max_trades_per_day` via rule JSON path (R1s-style strategy).
2. Replay on runtime VM; compare trade count and avg `net_pnl_pct` to rules cell.
3. Partial exits / scale-out → future engine work, not in rules sim today.
