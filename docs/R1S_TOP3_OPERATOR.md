# R1S Top-3 Paper Operator Sheet

**Rule:** `R1S_TOP3_S3_COMPOSITE` — sell ATM CE on ORB-down fade, max **3 entries/day**, ranked by `0.5*|ret_5m| + 0.5*|vwap_distance|`.

**Backtest artifact:** `ml_pipeline_2/artifacts/rules_runs/r1s_top3_20260520/` (76 cells, full corpus 2020-08 → 2024-10).

---

## Start paper runtime

```bash
python -m strategy_app.main \
  --engine deterministic \
  --strategy-profile-id r1s_top3_paper_v1 \
  --rollout-stage paper \
  --min-confidence 0.50
```

Requires live snapshots with ml_flat fields: `ctx_opening_range_*`, `ret_5m` / `fut_return_5m`, `vwap_distance` / `price_vs_vwap`.

---

## Manual week gate (required)

The engine does **not** auto-detect macro stress. Operator decision each week:

| Week | Action |
|------|--------|
| **ON** | VIX calm (~&lt;17–18), no macro shock, drift/chop acceptable |
| **OFF** | Elevated VIX, event risk, trending crash — do not run |

---

## Intraday limits (enforced in code)

| Limit | Value |
|-------|-------|
| Max new entries / day | 3 |
| Entry window | 9:30–14:30 IST (disqualifier) |
| No expiry-day entries | yes |
| Stop / target (short premium) | 100% / 50% of credit |
| Time stop | ~20 bars (~20 min) |
| Position side | **SHORT** CE (premium down = profit) |

---

## Streaming vs backtest

Backtest ranks **all** signals for the day, then keeps top 3. Paper uses **streaming top-3**: a bar enters only if it is in the top three candidates seen so far that day (same score sort, earlier minute wins ties). Usually matches batch; may differ on ~few days when a later stronger fade appears after an earlier entry.

---

## What to watch in logs

- `strategy_name=R1S_TOP3_SHORT_CE`
- `raw_signals._r1s_top3_score`, `_r1s_top3_rank_slot`
- `position_side=SHORT` on entry signals
- JSONL under strategy run dir via `SignalLogger`

---

## Execution note

Broker layer must **sell / write** ATM CE. The strategy emits CE direction with `_r1s_short_ce=true`; do not buy CE on entry.
