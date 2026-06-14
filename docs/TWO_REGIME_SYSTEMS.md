# The Two Regime Systems — Definitive Reference

> **Why this doc exists:** confusing these two systems caused months of wrong
> analysis (e.g., believing `REGIME_ALLOWED=MID,TREND` blocked BREAKOUT entries,
> or that the direction quality gate and the entry-strategy routing gate were the
> same thing). They are completely separate.

---

## System 1: `Regime` Enum — Entry Strategy Routing

**Where defined:** `strategy_app/engines/regime_classifier.py`  
**Used by:** `deterministic_rule_engine.py` profile dispatch

**Values:** `TRENDING`, `SIDEWAYS`, `BREAKOUT`, `AVOID`, `CHOP`, `PRE_EXPIRY`,
`EXPIRY`, `HIGH_VOL`, `PANIC`, `DEAD_MARKET`

**What it controls:** which entry *strategies* are available for this bar, via the
profile map in `strategy_app/engines/profiles.py`. Example for
`trader_master_live_v1`:

```
TRENDING   → [ML_ENTRY, ...]
SIDEWAYS   → [ML_ENTRY, ...]
BREAKOUT   → [ML_ENTRY, ...]   ← BREAKOUT IS allowed for entry
AVOID      → []                 ← blocks all entries
CHOP       → []                 ← blocks all entries
```

**Also controls exit stack routing** when `EXIT_STRATEGY_MODE=adaptive`:
- BREAKOUT + TRENDING → **lottery stack** (20% stop, `LOTTERY_HARD_STOP_PCT`)
- Everything else → **scalper stack** (`EXIT_SCALPER_HARD_STOP_PCT`)

**Classified by:** ATR expansion, volume breakout, prior day levels, session state.
Emitted per bar into `snapshot.regime`.

---

## System 2: `RegimeDirector` Quality — Direction Confidence

**Where defined:** `strategy_app/brain/regime_director.py`  
**Used by:** `entry_direction_policy.py` (the shared direction gate for both
ML_ENTRY and VOL_GATE_ENTRY)

**Values:** `TREND`, `MID`, `CHOP`

**What it controls:** whether the engine has enough directional conviction to
take a side (CE vs PE). Gated by `REGIME_ALLOWED=MID,TREND` in `entry_direction_policy.py`.

```
TREND  → direction is clear; entry side is taken
MID    → moderate confidence; entry side is taken
CHOP   → direction is ambiguous; entry is blocked (ABSTAIN returned)
```

In `BRAIN_DUAL_MODE=live`, the entry is blocked unless:
1. `verdict.side` is CE or PE (not ABSTAIN)
2. `quality` is in `REGIME_ALLOWED` (not CHOP)

**Classified by:** `mtf_derived.trend_regime` → 'TREND_UP'/'TREND_DOWN'/'MIXED'
mapped to quality levels. Not the same as the Regime enum.

---

## How They Interact

```
Bar arrives
  │
  ├─► Regime Classifier  →  Regime enum (TRENDING/BREAKOUT/AVOID/...)
  │         │
  │         ├─ Profile dispatch: "which strategies are allowed?"
  │         │   AVOID/CHOP → empty list → no entry (System 1 blocks)
  │         │
  │         └─ Exit routing: "scalper or lottery stack?"
  │             BREAKOUT/TRENDING → lottery (20% stop)
  │             else             → scalper
  │
  └─► RegimeDirector     →  Quality (TREND/MID/CHOP)
            │
            └─ Direction gate: "CE, PE, or ABSTAIN?"
                CHOP → ABSTAIN → entry blocked by direction policy (System 2 blocks)
                TREND/MID → side taken (subject to margin check)
```

**Key point:** both systems can independently block an entry. A BREAKOUT bar (allowed
by System 1) that has CHOP quality (blocked by System 2) still results in no trade.

---

## Common Mistakes

| Mistake | Reality |
|---|---|
| "`REGIME_ALLOWED=MID,TREND` prevents BREAKOUT entries" | No — it gates direction QUALITY, not the Regime enum. BREAKOUT entries are allowed by the profile; they're only blocked if RegimeDirector returns CHOP. |
| "Setting `EXIT_SCALPER_HARD_STOP_PCT=0.05` protects BREAKOUT entries" | No — BREAKOUT routes to the lottery stack; scalper stop has zero effect on them. Also set `LOTTERY_HARD_STOP_PCT`. |
| "CHOP regime = direction quality CHOP" | No — `Regime.CHOP` is the **entry strategy router** (no strategies available), while RegimeDirector quality=CHOP is the **direction gate**. Both use the word "chop" for different things. |
| "combo/agreement_lever is better than weighted because it requires consensus" | No — both `combo` and `agreement_lever` hard-block when ANY trio member is absent (OI/max_pain absent ~46% of bars in 2024 → ABSTAIN always). `weighted` treats absent members as 0 contribution (graceful). |

---

## Config Keys for Each System

### System 1 (Regime enum / routing)
| Key | Effect |
|---|---|
| `STRATEGY_PROFILE_ID` | Selects the profile (and its regime→strategy map) |
| `EXIT_STRATEGY_MODE` | `scalper` / `lottery` / `adaptive` (adaptive routes by Regime enum) |
| `ADAPTIVE_LOTTERY_REGIMES` | Comma-separated Regime enum values routed to lottery. Default: `BREAKOUT,TRENDING` |
| `LOTTERY_HARD_STOP_PCT` | Hard stop for lottery stack (affects BREAKOUT/TRENDING when adaptive). Default 0.20 |
| `EXIT_SCALPER_HARD_STOP_PCT` | Hard stop for scalper stack only. Does NOT apply to BREAKOUT/TRENDING in adaptive. |

### System 2 (RegimeDirector quality / direction)
| Key | Effect |
|---|---|
| `REGIME_ALLOWED` | Comma-separated quality values allowed to take a directional entry. Default: `MID,TREND` |
| `REGIME_DIRECTION_SIGNAL` | `weighted` / `combo` / `agreement_lever`. Controls how direction votes are aggregated. |
| `REGIME_W_MOM` | Weight for `momentum_15m` in weighted detector. Set to `0` — it is an ANTI-signal (48.1% acc). |
| `REGIME_W_VWAP` | Weight for VWAP signal. Mildly anti (50.5%) — consider reducing. |
| `REGIME_W_MAXPAIN` | Weight for max pain signal (51.2% acc, H2 52.8%). Positive contributor. |
| `REGIME_W_OI` | Weight for ATM OI signal (52.1% acc, best individual). Keep. |
| `REGIME_W_EMA` | Weight for EMA signal. Modest positive contributor. Keep. |
