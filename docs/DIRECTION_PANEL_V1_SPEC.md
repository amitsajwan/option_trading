# Direction Panel v1 — Implementation Spec

**Status:** DESIGN — validated in sim, NOT implemented. Build gated on the confidence-gate sweep + paper validation.
**Date:** 2026-06-09
**Owner:** strategy / direction
**Problem it solves:** Direction (CE vs PE side selection) is the strategy's bottleneck. The live `composite` engine picks the right side only ~53% (coin-flip), which causes ~2× of all losses and keeps the system −EV.

---

## 1. Evidence base (what we validated, 8 sessions: IS = Jun 1–5, OOS = May 26/27 + Jun 8, 2026)

| Finding | Result | Confidence |
|---|---|---|
| ML `consensus` direction beats `composite` | win 44%→**59%**, net −12%→**−3.6%** (same 0.80 entry, 34 trades) | A/B, decisive |
| ML model confidence is predictive | 55–66% overall; **73–77%** at `\|p−0.5\|≥0.10` (OOS) | measured, small n |
| Market mean-reverts: **fade > continuation** | r5m_fade 53/59%, r15m_fade 55/53%, breakout_fade 57/75% (IS/OOS); continuation 43–25% | validated IS+OOS |
| `vwap`, `iv_skew`, `orb_reject`, `swing` | ~50% or anti-predictive | confirmed noise/anti |
| Regime guard lifts accuracy | **60% IS / 68% OOS** with 52–60% coverage | validated, small OOS |
| Structural "Bloom" elimination rules | do NOT generalize OOS — only ML-confidence survives | negative result |
| "Entry lenient" | **trades junk** (20% win, −34%) — entry MUST stay selective | refuted |

---

## 2. Target design — the Direction Panel

**4 active members + 2 gating overlays.** (Current panel has ~6–10 members with the ML model badly underweighted at 0.15.)

### 2a. Members (weighted blend → net CE/PE conviction)
| Member | Weight | Signal | Evidence |
|---|---|---|---|
| `ml_direction` (`ml_ce_prob`) | **0.55** | the trained direction-only model | 55–66%, 73–77% high-conf |
| `fade_r15m` | **0.20** | PE if r15m>0 else CE (fade 15m momentum) | 54.9/52.8% |
| `fade_r5m` | **0.15** | PE if r5m>0 else CE | 52.8/58.7% |
| `fade_breakout` | **0.10** | PE on breakout_up, CE on breakout_down | 56.8/75% (thin n=56) |

> Weights are a **starting point from ~7 days** — must be re-fit by weight-optimization + OOS before being trusted. `fade_breakout` kept low until more data.

### 2b. DROPPED members (do not include)
`vwap` (50% noise), `iv_skew` (noise), `orb_high_reject`/`orb_low_reject` (43.5%/anti), `swing`/trend-following (44.5% anti), and momentum-as-**continuation** (replaced by its fade).

### 2c. Overlay 1 — Regime guard (gates whether the panel acts)
```
if regime == AVOID:                                   abstain
elif opening_range_width_pct >= 0.008:                abstain   # expansion/event days kill the edge
elif regime == CHOP:                                  use FADE-weighted panel (lean fade members)
elif regime in {SIDEWAYS, BREAKOUT, EXPIRY} and regime_confidence >= 0.70:  use ML-weighted panel
elif regime == TRENDING:                              abstain   # too rare/fragile (≈0 OOS bars)
else (low-conf SIDEWAYS/BREAKOUT):                    abstain
```

### 2d. Overlay 2 — Confidence gate (selectivity / cost lever)
Only take the trade when the panel's net conviction clears a threshold. Fewer, higher-conviction trades → cuts the ~0.6%/trade cost bleed.

> **⚠️ VERIFIED 2026-06-09: `DIRECTION_ML_FILTER_MIN_PROB` is INERT in consensus mode.** The sweep (none/0.55/0.60/0.65) returned byte-identical results (34 tr / 59% / −3.6%) — the engine's Mode-2 ML-direction filter (`strategy_app/ml/direction_ml_policy.py`) is NOT in the `consensus` direction code path. **The confidence gate is therefore a CODE task, not a config flip** — wire an `ml_ce_prob`-based min-confidence filter into the consensus resolver (block/abstain when the chosen-side prob < threshold). Moved to Phase 2.

### 2e. Entry stays SELECTIVE
Keep `STRATEGY_MIN_CONFIDENCE` at the live level (~0.80). **Do NOT loosen entry** — the entry model's 82%-catch-a-move edge only exists at a high threshold (lenient entry = 20% win / −34%).

---

## 3. Implementation phasing

### Phase 1 — MVP (config only, ships fastest, already A/B-validated → 59% / −3.6%)
- `ML_ENTRY_DIRECTION_MODE=consensus` (ML model leads — equivalent to ml_direction weight ≈ 1.0)
- Keep entry selective (~0.80); 1 lot; existing safe exits.
- **No new code.** Pure `.env.compose` change on `/opt/option_trading/.env.compose` (the canonical File A).
- **NOTE:** Phase 1 alone is still ~−3.6% net (no working confidence gate — see §2d). It is a *risk-reduction* step (better direction, fewer wrong-side losses), not yet profitable. Do NOT expect net-positive from Phase 1 — that needs Phase 2's coded gate.

### Phase 2 — Full panel + fades (needs code)
- Add `fade_r15m`, `fade_r5m`, `fade_breakout` as direction members and blend with `ml_direction` at the weights above. Touch the direction-consensus resolver (the module that builds `direction.source`). Expose weights as env (`DIRECTION_PANEL_*_WEIGHT`).
- Re-fit weights via optimization on a larger window; ship-gate on OOS.

### Phase 3 — Regime guard (needs code)
- Implement §2c in the direction path, reading `regime_context.regime`, `.confidence`, `features.opening_range_width_pct`. Make the thresholds env-configurable. Emit the guard decision into the decision trace.

---

## 4. Validation gates (must pass before ANY live change)
1. **Sim, IS + OOS:** the chosen config is **net-positive after costs** on a non-trivial trade count (not a 6-trade fluke), and holds in OOS.
2. **Paper, ≥3–5 sessions:** live-paper book confirms the sim result (win-rate + net) on fresh data.
3. **Only then** flip the live direction config. Real money stays 1 lot.

## 5. Rollout
`Phase 1 config in sim → paper → (if confirmed) live config flip` → then build Phase 2/3 and repeat the gate each time. Live engine stays strict/minimal (near-zero bleed) until a config clears gate #1 and #2.

## 6. Risks / open questions
- **Small sample (~7 days):** weights & gate value are provisional; mean-reversion can flip in a sustained trend → the regime guard is the safeguard but is itself OOS-thin.
- **`ml_ce_prob` coverage:** populated only in consensus mode and not on every day in the traces — confirm it's present every session live.
- **Per-bar ≠ per-trade:** 73–77% per-bar direction did not fully carry to trade P&L in the spot-check; exits matter.
- **`fade_breakout`** OOS n=56 — under-weight until validated.

## 7. Related
`docs/` — see memory: direction member analysis, entry-vs-direction decomposition, go-live config. The canonical live config is `/opt/option_trading/.env.compose` (File A); the sim mirrors it via `ops_env.json` but omits `RISK_LIVE_MIN_GRADE` (pass as override).
