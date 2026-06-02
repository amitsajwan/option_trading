# 00 — Context & Findings

*The evidence base. Everything here is from real traces, MongoDB, and verified
simulations on 2026-06-01 data. Read this before touching strategy logic.*

---

## 1. The core trading problem

The **entry/direction model is good. The exit was destroying the edge.**

2026-06-01 live session, original config (TIME_STOP-only exit):
```
8 trades · 25% win rate · -3.61% session
6 of 8 trades went into profit, then reversed to a loss before exit.
Capture ratio: -96%  (gave back more than the favorable move offered)
```

The direction model found the move 6 times out of 8. The exit (hold N bars then close
at market) is mathematically guaranteed to give back winners. That single fact drove
all the exit-strategy work.

---

## 2. Two philosophies, now both implemented

| | Scalper | Lottery |
|---|---|---|
| Trades/day | ~12 | ~3 |
| Entry bar | confidence ≥ 0.65 | confidence ≥ 0.80 |
| Exit | tight: trail 0.5%, target capped | wide: hard stop, big target, loose runner trail |
| Win rate | higher | lower (accepts many small losses) |
| Payoff shape | many small symmetric outcomes | asymmetric — lose small often, win big rarely |
| Code | `build_scalper_exit_stack()` | `build_lottery_exit_stack()` |
| Selector | `EXIT_STRATEGY_MODE=scalper` | `EXIT_STRATEGY_MODE=lottery` |

Both live in `strategy_app/position/exit_policy.py`. `build_default_exit_stack()`
branches on `EXIT_STRATEGY_MODE`.

---

## 3. Verified findings (with the data)

### 3.1 The exit was the problem, not the entry
On 2026-06-01 the 54300 PE (the actual trade) moved:
```
entry @10:02 = 1116    intra-hold peak = 1162 (+4.15% MFE)    day peak 1380 (+24%)
We exited @10:14 at +0.52%.
```
Direction right, capture terrible.

### 3.2 Scalper exit stack works (sim, same-day, same entries)
Replaying 2026-06-01 with the scalper stack (`ThesisFail → TrailingStop → PremiumTarget`):
```
12 trades · 75% win · +6.58%   vs actual -3.61%
```
Same entries, same direction model — just exits that don't surrender the edge.
**Verified to the decimal:** entry/exit/MFE recomputed independently from raw snapshot
LTP series matched the engine's numbers exactly.

### 3.3 Smart-strike was silently disabled by an IV units bug
`option_selector.py` compared `snap.iv_percentile` (0–100) against tier ceilings
`*_IV_CEIL` that were set to 30–60 (absolute-IV-looking values). Any day with IV
percentile > 60 (most active days) rejected **all** OTM tiers → forced ATM every time.
2026-06-01 IV was at the 86th percentile, so the system was ATM-locked.
**Fix:** ceilings re-expressed as percentile thresholds (89–92).

The selector also caps at OTM-4 (no OTM5–8), so the genuinely cheap ₹400–800 strikes
(6–9 steps out) are unreachable. `STRATEGY_STRIKE_MAX_OTM_STEPS` is currently ignored
by the tier builder — see backlog E-STRIKE.

### 3.4 "Let it run" naively LOSES — the runner trail is essential
Lottery parameter sweep on 2026-06-01, trade 1 (54000 PE, +12% intra-hold MFE):
```
timestop=25b           +5.55%   (cut near a local high — lucky)
timestop=45b           +3.73%   (held to +12% then gave back)
timestop=60b           -2.35%   (round-tripped +12% → loss)   ❌
timestop=90b           -0.66%   (round-tripped)               ❌
90b + runner(act10%,give35%)  +5.47%  (trail LOCKED the gain)  ✅
```
**Conclusion:** holding for the big move means sitting through reversals. Pure
"hold longer" round-trips. The `RunnerTrailPolicy` (loose giveback, only after a big
MFE) is what makes "let it run" safe.

### 3.5 One day cannot validate a lottery strategy
Lottery's whole premise is that rare big-move days pay for many small-loss days.
2026-06-01 had no big tail (max +12%), so lottery (+5.65%, 3 trades) ≈ scalper
(+6.58%, 12 trades). **The lottery edge can only be proven over 20+ days.** This is
project #02 — [Multi-Day Sim](02_MULTI_DAY_SIM.md).

---

## 4. Sim-fidelity bugs found and fixed (critical institutional knowledge)

A simulation is worthless if it diverges from live. Four real divergences were found:

1. **ML library drift.** Dashboard had pandas 3.0 / sklearn 1.8; live had pandas 2.x /
   sklearn 1.7.2. The entry-model features computed differently → the sim produced
   **zero trades**. Fix: pin the dashboard ML stack to match `strategy_app`.
2. **Config from wrong process.** The sim read config from the dashboard's `os.getenv`
   (which lacks strategy_app's vars), defaulting `RISK_MAX_SESSION_TRADES=6` and capping
   the day at 6 trades instead of the live 12. Fix: strategy_app writes `ops_env.json`
   to the shared `.run` volume; the sim reads that as the baseline.
3. **Risk config overwrite.** The sim *overwrote* the profile's `risk_config`, wiping
   `allow_non_atm_for_ml_entry` → smart-strike never fired. Fix: *merge* like `main.py` does.
4. **Sim wrote to live JSONL.** A standalone sim run inside the live container used
   `setdefault(STRATEGY_RUN_DIR)` which no-ops when the live path is set → 72 sim trades
   leaked into the live `positions.jsonl`. Fix: force the run dir; exclude `sim-*` run_ids.

**Rule for the new team:** any time you run the engine outside live, prove it produces
identical output to live on a known day *before* trusting its numbers. See
[Architecture §Sim Fidelity](01_ARCHITECTURE.md).

---

## 5. Other fixes shipped (context, not open work)

- `run_id` was `null` in Mongo → unique session run_id per startup.
- Direction ML model reloaded every snapshot → loads once at startup.
- Dashboard chart axis showed UTC (03:45) → shifted to IST (09:15).
- Dashboard: open positions now shown, ENGINE label real, depth reads Redis live.
- Telegram alerts, Kite token auto-refresh, daily P&L report, live cutover runbook.

---

## 6. Open strategic questions for the new team

1. Does lottery beat scalper over 20+ days, on cumulative P&L *and* max drawdown? (#02)
2. Can we reach genuinely cheap strikes (₹400–800)? Requires OTM5–8 tiers + liquidity
   check. Is BANKNIFTY deep-OTM liquid enough intraday?
3. Should scalper and lottery run *concurrently* on the same data, as separate books? (#03)
4. What is the right per-strategy capital/risk allocation when multiple strategies run?
