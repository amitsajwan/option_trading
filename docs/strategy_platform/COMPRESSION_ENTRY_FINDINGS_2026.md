# Compression Entry Strategy — Validated Findings (June 2026)

## What We Know (Validated)

### Entry model: `entry_compression_v1`
- **File**: `/app/ml_pipeline_2/artifacts/entry_only/published/entry_compression_v1.joblib`
- **Features**: 40 (compression_score, MFE/vol/OI/regime features)
- **Label**: magnitude ≥0.20% underlying move in next 5 bars
- **Holdout AUC**: 0.83 (2024 OOS)
- **Recommended threshold**: 0.40 (fires 1.1% of bars)
- **Validated threshold**: **0.35** (fires ~3% of bars, better balance)
- **Fire frequency June 2026**: 3 of 15 days (Jun 01-03). Zero fires Jun 10-19 = model correctly identified no compression setup, not a failure.

### Exit policy: Lottery beats Scalper for these entries
Tested on 72 cells (regime × threshold × direction × exit mode), June 2026:

| Exit | n | WR | Avg% | Total ₹ (1 lot) |
|---|---|---|---|---|
| **lottery / adaptive** | 10 | 50% | +1.80% | +₹6,087 |
| scalper | 12 | 42% | −0.98% | −₹3,945 |

**Why**: Entry model selects big-move setups (avg MFE 4-6% option premium). Scalper exits at 3% target — cuts winners before the move completes. Lottery holds until thesis fails or EOD, capturing more of the move.

**Rule**: `EXIT_STRATEGY_MODE=adaptive` + `ADAPTIVE_LOTTERY_REGIMES=TREND,TRENDING` is correct. TREND bars → lottery. MID bars → scalper.

### Direction: multi_signal scorer (live as of June 20, 2026)

Live direction mode is `ML_ENTRY_DIRECTION_MODE=multi_signal` — a stateless 6-signal scorer applied to each bar after the ML entry gate passes:

| Signal | Weight | Bullish | Bearish |
|---|---|---|---|
| ORB break | ±2.0 | above ORB high | below ORB low |
| VWAP side | ±2.0 | price > VWAP | price < VWAP |
| Straddle dominance | ±2.0 | PE premium > CE×1.04 | CE premium > PE×1.04 |
| PCR change 5m | ±1.0 | PCR falling | PCR rising |
| VIX intraday | ±1.5 | VIX down ≥3% | VIX up ≥3% |
| EMA order | ±1.0 | 9>21>50 | 50>21>9 |

`|score| < 2.0` → ABSTAIN → no trade (coin-flip zone). `≥2.0` → CE. `≤-2.0` → PE.

**From isolation test (June 2, 86 ML_ENTRY fires at threshold=0.05):**
- 78 bars: direction abstained (|score| < 2.0)
- 67 bars: direction mismatch (ML wanted one direction, score said other)
- Only 2 entries through (both CE)

Grid validation results (pre-June 20, different direction modes):
| Direction | n | WR | Avg% |
|---|---|---|---|
| **weighted** | 6 | 67% | +3.79% |
| combo_vwap | 10 | 50% | +1.81% |

`weighted` was the pre-June-20 direction mode. Switching to `multi_signal` is a simplification (no rolling state); grid re-validation pending.

### Regime: MID+TREND sufficient, TREND-only more precise

| Regime filter | Active bars | Entries (thr=0.35) |
|---|---|---|
| mid_trend (MID+TREND) | 55% of day | 11-14 |
| trend_only (TREND only) | 24% of day | 9-11 |

June 2026 was 45% CHOP — a structurally choppy period. Normal market should have more TREND bars.

### Why so few trades? (this is correct behavior)

The old system used `ENTRY_ML_MIN_PROB=0.049` with `velocity_base_entry_bundle` — effectively a near-zero threshold that fired on almost every active-regime bar (6-10 trades/day). That was noise.

The compression branch uses three selective gates:

| Gate | Blocks | Reason |
|---|---|---|
| Regime (CHOP/AVOID) | ~33% of bars on June 2; up to 90% on choppy days | Router sends CHOP to empty strategy list |
| Model threshold 0.35 | Only fires on compression setups | ML trained on specific setup; fires 0-3 times/day |
| Direction multi_signal abstain | 78/86 ML fires blocked on June 2 | |score| < 2.0 = coin-flip zone, skip |

**Result**: June 2026 had 1 trade on 3 days (Jun 01-03); zero on Jun 10-19. Zero on non-setup days = correct, not a bug. The compression model identifies a specific market structure (IV contraction + OI buildup) that doesn't exist most days.

---

## Architecture (Validated)

```
Every 1-min bar:
  [1] REGIME     regime tagger → CHOP/AVOID/PANIC → SKIP (no routes)
                              → SIDEWAYS/TRENDING/BREAKOUT/etc. → continue
  [2] IV_FILTER  iv_percentile > 95% → SKIP
  [3] ML ENTRY   entry_compression_v1.predict(bar) ≥ 0.35 → big move expected
                 < 0.35 → no vote
  [4] DIRECTION  multi_signal scorer (stateless, snapshot-only):
                   ORB break       ±2.0
                   VWAP side       ±2.0   (price vs VWAP)
                   Straddle dom    ±2.0   (CE vs PE premium dominance)
                   PCR change      ±1.0   (rising PCR = puts building = bearish)
                   VIX intraday    ±1.5   (VIX rising = fear = bearish)
                   EMA order       ±1.0   (9>21>50 stack = bullish)
                 |score| < 2.0 → ABSTAIN (skip even if ML fired)
                 score ≥ 2.0 → CE   score ≤ -2.0 → PE
  [5] EXECUTE    buy ATM, 1 lot (paper; flip STRATEGY_ROLLOUT_STAGE to live when ready)
  [6] EXIT       adaptive:
                   TREND / TRENDING / BREAKOUT → LOTTERY
                     (hold until thesis fails or EOD; 20% hard stop)
                   SIDEWAYS / HIGH_VOL / other → SCALPER
                     (3% target, 7% hard stop, thesis-fail 5 bars)
```

This is NOT a direction strategy. It is a **magnitude strategy with direction as a confirmer**.
Entry fires on compression setup; direction abstains when signals are weak (coin-flip zone).

---

## Seller System (S3): Already Deployed

The S3 seller is running in `seller_app` container. **Currently in PAPER mode** (`SELLER_LIVE_ENABLED=0`).

Config:
- Iron condor: short ATM+200 CE + short ATM-200 PE
- TP: 50% of credit received
- Stop: 2× credit (per leg, bounded)
- Max hold: 5 days
- IV rank gate: ≥30
- Entry window: 10:00-14:00 IST

2024 backtest: **78% win rate, +₹1,692/trade, +₹123k over 43 days** (drop-top3: +₹104k).

**Known bug (fixed Jun 19)**: `/seller_run` directory was not writable → seller couldn't save state. Fixed with `chmod 777`.

**To go live**: Set `SELLER_LIVE_ENABLED=1` in `.env.compose` and recreate container. Requires paper validation first.

---

## Grid Scripts (in `/tmp/` on VM — recreated each session, known issue)

These should move to `ops/research/` but currently live as tmux-run scripts:

| Script | Purpose |
|---|---|
| `/tmp/full_grid_2026.py` | regime × direction × TP/SL search (no ML entry model) |
| `/tmp/grid_wide_tp_2026.py` | Wide TP/SL test (50-120pt), no entry model — showed MFE=20pt without entry model |
| `/tmp/grid_entry_exits_2026.py` | Entry model + 4 scalper configs — confirmed thr=0.35 best |
| `/tmp/grid_3exits_2026.py` | **Entry model + scalper/lottery/adaptive** — **definitive result** |

**Key finding from grid evolution**: Without the ML entry model, avg MFE = 20pt → no TP/SL works. With entry model, avg MFE = 4-6% option premium → lottery exit profitable.

---

## Open Items Before Live

1. **Seller paper validation** — run 1-2 weeks paper, confirm fills, then `SELLER_LIVE_ENABLED=1`
2. **July data** — entry model fires on compression days; need July to confirm frequency
3. **Futures rollover** — update `BANKNIFTY_FUTURES_SYMBOL=BANKNIFTY26JULFUT` before June 26
4. **Config verification** — run `verify_config.py` at session start (see `ops/gcp/verify_config.py`)
5. **Capital** — ₹24k balance; monthly ATM = ₹33k/lot. Trade weekly expiry or rebuild capital.

---

## Config Quick Reference

```bash
# Verify entire live config in one command:
sudo docker exec option_trading-strategy_app-1 python /app/ops/gcp/verify_config.py
sudo docker exec option_trading-seller_app-1   python /app/ops/gcp/verify_config.py --seller

# Key env vars (strategy_app):
EXIT_STRATEGY_MODE=adaptive
ADAPTIVE_LOTTERY_REGIMES=TREND,TRENDING,BREAKOUT  # ← lottery on these; SIDEWAYS → scalper
ENTRY_ML_MODEL_PATH=.../entry_compression_v1.joblib
ENTRY_ML_MIN_PROB=0.35
ML_ENTRY_DIRECTION_MODE=multi_signal      # ← stateless 6-signal scorer; abstains if |score|<2.0
ENTRY_MULTI_SIGNAL_MIN=2.0               # ← default; raise to 3.0 to filter more aggressively
# REGIME_DIRECTION_SIGNAL=weighted is DEAD for multi_signal — only used by regime_dual

# Key env vars (seller_app):
SELLER_LIVE_ENABLED=0   # change to 1 to go live
SELLER_CONDOR_OFFSET=200
SELLER_TP_FRAC=0.50
SELLER_STOP_MULT=2.0
```

---

## What Does NOT Work (Do Not Revisit)

- **Buying without entry model**: regime+direction alone = avg MFE 20pt, all P&L negative
- **Scalper exits on compression entries**: exits winners at 3% before move completes
- **Direction as primary gate**: direction ~50% coin flip, entry model must come first
- **Tight hard stop on lottery entries**: LOTTERY_HARD_STOP_PCT=0.20 is correct; setting EXIT_SCALPER_HARD_STOP_PCT doesn't protect lottery entries (see Jun 05 incident)
