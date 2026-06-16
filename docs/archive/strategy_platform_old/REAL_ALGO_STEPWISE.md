# The Real Algorithm — Step by Step (as it runs live, 2026-06-14)

*Profile `trader_master_live_v1`. BANKNIFTY, 1-minute bars, weekly options, BUY
side (long CE/PE). Verified against the live container config. Every number here
is a config key in `ops/strategy_config.yml` (the single source of truth).*

> One-line summary: **find a volatility bar → confirm a direction → buy an OTM
> option → manage the exit by regime.** The volatility trigger is the binding
> gate; direction is the weak link; cost (~1%) is the tax.

---

## Per-bar pipeline (runs every 1 minute)

### STEP 0 — Snapshot
Ingestion builds the 1-min snapshot: futures bar (`fut_close`, `fut_volume`) +
25-strike option chain + derived features (`atr_14_1m`, vwap, OI, max_pain, IV,
`atm_straddle_price`, etc.).

### STEP 1 — Regime classification  → routes everything
Classify the bar into a Regime: **TRENDING / BREAKOUT / SIDEWAYS / HIGH_VOL /
CHOP / PRE_EXPIRY / EXPIRY / AVOID**. Drivers: trend score ≥ `REGIME_TREND_SCORE_MIN`
(2.0), `REGIME_TREND_VOL_RATIO_MIN` (1.30), aligned 5/15/30m returns. The regime
(a) decides if entries are allowed (**CHOP/AVOID → no entry**) and (b) selects the
exit stack later.

### STEP 2 — Entry eligibility (HardGates)
Valid entry phase · risk not halted/paused · inside `ENTRY_TIME_WINDOWS`
(**09:45–14:30 IST**) · regime allows entry. Fail → bar dead.

### STEP 3 — Strategy votes (the producers)
For `trader_master_live_v1` the active set per tradeable regime is
`[IV_FILTER, ML_ENTRY]`, and `ENTRY_VOL_GATE_ENABLED=1` swaps `ML_ENTRY → VOL_GATE_ENTRY`:
- **IV_FILTER** — veto the bar if IV percentile is extreme.
- **VOL_GATE_ENTRY** (the trigger) — fires an ENTRY vote **iff
  `atr_14_1m / fut_close ≥ ATR_ENTRY_MIN_PCT` (0.00088)**. No fire → `no_strategy_votes`
  → bar dead. **This is the binding gate** — on quiet days nothing fires (the
  June-12 finding). Direction is attached by the shared resolver (Step 4.5).

### STEP 4 — Entry pipeline gates (in order)
1. **HardGates** — re-check phase/risk/time.
2. **Votes** — ≥1 ENTRY vote exists.
3. **RegimeConfidence** — regime confidence ≥ min (unless profile relaxes).
4. **MLEntry** — the trigger vote clears `CONSENSUS_BYPASS_MIN_CONFIDENCE` (0.65).
   *(ML-first: split out so a bar that fails here never reaches direction.)*
5. **Direction** — resolve **CE vs PE** via `ML_ENTRY_DIRECTION_MODE=regime_dual`
   = **40% ML direction model + 60% composite** (vwap / momentum_15m / OI / max_pain /
   ema, weighted). Veto if mixed/abstain. *(This is the weak, ~coin-flip link.)*
6. **StrikeDepth** — `STRATEGY_STRIKE_SELECTION_POLICY=otm`: pick an **OTM strike in
   the ₹600–1300 premium band**, up to `STRATEGY_STRIKE_MAX_OTM_STEPS` (12) OTM;
   IV-ceiling veto; hard premium cap. Writes strike + entry premium.
7. **EntryPolicy** — final policy checks (skip candidate if blocked).
8. **Confidence** — candidate confidence ≥ `STRATEGY_MIN_CONFIDENCE` (0.65).

### STEP 5 — Risk sizing
`lots = 1` (`RISK_MAX_LOTS_PER_TRADE=1`), capital ₹41,000, `RISK_PER_TRADE_PCT=0.5%`.
Session caps: **6 trades/day**, halt after **6 consecutive losses**.

### STEP 6 — Emit the trade
- **Paper / sim** → record a position in the paper book (no broker).
- **Live** → publish ENTRY to the trade-signal topic → `execution_app` →
  **tier gate** (only `tier==live`) → **sim-block** (run_id `sim-*` rejected) →
  Dhan adapter. *(In June every real order was rejected `Invalid IP` — whitelist
  unresolved; so live has effectively been paper.)*

### STEP 7 — Position management (each later bar) — the exit stack
`EXIT_POLICY_STACK_ENABLED=1`. Outer-to-inner:
- **Universal loss floor (always):** `EXIT_MAX_LOSS_PCT=10%` → hard cut at −10%.
- **Adaptive routing by the entry regime:**
  - **TRENDING / BREAKOUT → LOTTERY stack:** target **+50%**, hard stop 20% (the 10%
    floor fires first), runner trail activates at **+20% MFE** / gives back 35%,
    thesis-fail **disabled** (999), **timestop 90 bars**, momentum-flip exit at 1.0.
  - **SIDEWAYS / HIGH_VOL / CHOP → SCALPER stack:** hard stop **7%**, target **3%**,
    trail activates +1.5% / trails 0.8%, thesis-fail **disabled** (999).

### STEP 8 — Close
Record exit + reason: `TARGET_HIT / TIME_STOP / TRAILING_STOP / THESIS_FAIL /
REGIME_SHIFT / max-loss`. Compute P&L.

---

## Where the edge is (and isn't) — from the forensics

| Stage | Verdict |
|---|---|
| **Entry magnitude** (vol trigger) | **Strong** — reliably finds bars that move (entry model AUC ~0.83; vol-gate matches it). |
| **Direction** | **Weak / non-stationary** — ~coin-flip (quorum 50.3% on 37k 2024 bars; inverts to 43.9% in 2026). `momentum_15m` is an anti-signal. THE cap. |
| **Exits** | `adaptive` confirmed best by sweeps. |
| **Cost** | ~1% round-trip (~108 pts) — dominates flat scalps. |
| **Net (clean live ledger)** | 31 trades / 4 days, +0.965%/trade **pre-cost** → **break-even-to-negative after cost**, and the total is one outlier (June-12 +32.5%). No robust buy-side edge. |

**Why the redesign matters:** STEP 3's absolute `0.00088` gate is an *elimination*
filter (0 trades on quiet days). The proposed **opportunity gate** replaces it with
*score → rank → select* (relative to recent days) + cost floor + budget, and turns
STEP 5's direction into *sizing* (CE / PE / **straddle** when direction is weak) —
because the volatility edge is real and the direction edge isn't. See
`OPPORTUNITY_GATE_DESIGN.md` and `GATE_FORENSICS_AND_CONFIG.md`. That path is
SIM-validated but not yet wired or P&L-proven.

---

## SIM vs LIVE (what the UI runs)
The OPS-panel SIM runs this exact pipeline in-process with the **same config**
(via `ops/strategy_config.yml`), but forces `EXECUTION_ADAPTER=paper`,
`STRATEGY_REDIS_PUBLISH_ENABLED=0`, and a `/tmp` run dir — so it **never touches
the broker, live topics, or live state**. SIM ≡ LIVE on decisions, isolated on side effects.
