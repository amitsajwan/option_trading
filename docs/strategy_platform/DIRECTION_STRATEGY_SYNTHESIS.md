# Direction Strategy — Synthesis & Plan (2026-06-16)

> Written end-of-day 2026-06-15 to resume from tomorrow. Entry/move-detection is
> effectively solved; **direction is THE wall.** This doc consolidates everything we
> have proven about direction, the current thresholds/algo, and the best path forward.

---

## 1. The core truth

- **Entry (will a move come?) is NOT the problem.** 92% of entries get a ≥50pt move;
  the rolling entry model ranks moves well (served walk-forward AUC ~0.78). Move-detection ≈ ATR.
- **Direction (which side?) IS the entire bottleneck.** ~50–56% side accuracy. Buying loses
  after cost because the side is ~a coin flip and the ~108pt cost floor eats the edge.
- **Direction is non-stationary** — what works one period inverts the next. Any edge must be
  re-validated on the *current* regime (forward), not just backtested.

---

## 2. What we have PROVEN about direction (with sources)

| Finding | Result | Source |
|---|---|---|
| Side accuracy overall | ~50.3% = **coin flip** over 37k 2024 bars | momentum_antisignal_breakthrough |
| Per-member edge (633 big-move bars, Jun1-5) | **`vwap` is the ONLY edge (54.2%)**; `orb_high_reject` (43.5%) & `momentum_15m` (46.5%) are **ANTI-predictive (flip them)**; rest noise | direction_member_analysis |
| **The one OOS-validated lever** | **Big moves (≥150pt, 10min) + `momentum_15m` + `max_pain` + OI AGREEMENT → ~61%** (both 2024 halves). ABSTAIN unless 3 agree | direction_lever |
| 2026 direction tree | small/med moves = coin-flip & **invert early→late**; **BIG moves ≥160pt + vwap+pcr agree → ~59-62%** (only edge, thin n) | direction_tree |
| Quorum / consensus | 50.3% (coin flip); **2026 OOS quorum 43.9% (INVERTS)**; only 2/31 combos robust across both 2024 halves | momentum_antisignal_breakthrough |
| Recent-regime flow | 8-day window **mean-reverts**: flow-following 43%, **FADE-vwap 57%** → "align with flow" picks the losing side in that regime | dual_signed_entry |
| LLM over structural facts | **REFUTED** — Groq 56.9% ≈ vwap ceiling; LLM is a 79%-vwap-follower whose independent deviations are anti-predictive (48%); zero abstention | llm_direction_test |
| Direction ML model (v2) | best-ever AUC **0.593**, stable; soft-overlay @prob≥0.60 → 20% coverage, 64% acc; 3m horizon best | direction_v2 |
| Direction ML on taken trades | v2 WORSE than composite (43% vs 57%) — v2 sits near 0.5 exactly where we trade | paper_vs_live_tier |
| Only net-positive path | **S3 SELLER** (premium selling) — sidesteps direction entirely | seller_system_built |

**Distilled:** the *only* repeatable directional edge is **AGREEMENT of vwap + OI/max_pain (+pcr)
on BIG moves (≥150–160pt) → ~59–62%**, and even that is thin and can invert by regime. Everything
standalone (single member, consensus quorum, plain ML, LLM-on-structure) is ~coin flip.

---

## 3. What we BUILT for direction (assets on hand)

- **Conviction ensemble** — `entry_direction_policy._conviction_ensemble_direction()`: vwap +
  OR-break + straddle-expansion members, unanimous + veto-on-division, missing/disabled member
  excluded. Mode `ML_ENTRY_DIRECTION_MODE=conviction_ensemble`. (Built, not the live default.)
- **NEW rolling direction model** (training overnight 2026-06-15 on ML VM): per-bar CE-vs-PE,
  **09:45–15:00** (same as entry), **rolling 10-min velocity, leak-free, no empty features**
  (systematically-NaN feats pruned — the entry-model lesson). Label = up/down over 10 bars on
  directional bars (|move|≥40pt). Walk-forward 2020-2023 → 2024. Bundle:
  `~/direction_rolling_bundle.joblib` (kind `direction_only_bundle`, loads via `DIRECTION_ML_MODEL_PATH`).
  Reports **accuracy** (the metric that matters) overall + by move-size bucket + by confidence.
  **>> RESULT (holdout 2024, 51/57 feats — 6 empty dropped: 4 IV-velocity + ctx_am_vol_vs_yday +
  vol_spike_ratio):**
  - **Overall acc 53.3%** (AUC 0.5471, base 50%) — barely above coin flip.
  - By move size: 40-80pt **53.6%**, 80-160pt **53.6%**, **160+pt 49.8%** (big moves = coin flip!
    *contradicts* the old "big moves more predictable" lever — that needed the vwap+OI+max_pain
    AGREEMENT subset, not a plain model).
  - **Confidence-gated (the abstaining signal): conf≥0.55 → 54.8% (59% cov); conf≥0.60 → 57.0%
    (31% cov); conf≥0.65 → 56.5% (14% cov).**
  - **READ:** plain direction is the wall, BUT the model has a usable confidence signal — on its
    confident ~31% it hits 57%. Not yet break-even (~61% after cost) but a real foundation: abstain
    on the coin-flip ~69%, trade the confident subset, then lift it with agreement gating + regime
    re-validation. Bundle saved `~/direction_rolling_bundle.joblib` (686KB), NOT deployed.
- **Live direction node:** `resolve_direction_for_entry()` in `entry_direction_policy.py`
  (dispatch on `ML_ENTRY_DIRECTION_MODE`). The direction-only model plugs in as `ml_direction_ce_prob`.

---

## 4. What will be BEST (the plan to refine from)

Direction has no robust *standalone* edge, so the design must be **conditional + abstaining**:

1. **Abstain by default.** Only take a directional CE/PE when there is **agreement** on a **big move**.
   Otherwise → non-directional (STRADDLE) or no trade. This is the single most robust rule we have.
2. **Agreement ensemble (not consensus quorum).** Require **vwap + OI/max_pain + pcr** to agree,
   AND gate on **move size (≥150–160pt expected)**. Add the **new rolling direction model** as
   *one more confirmer* (require its P(CE) on the same side at conf ≥0.60). Abstain otherwise.
3. **Flip/exclude the known anti-signals** (`momentum_15m`, `orb_high_reject`) — they are
   anti-predictive; never let them vote raw.
4. **Re-validate on the CURRENT regime (forward).** Direction inverts; a 2024 edge may be negative
   in the live regime. Shadow the model live before trusting it; check it hasn't flipped sign.
5. **Test the one un-refuted NEW sense:** Gemini web-grounding (macro/news/RBI) as an *independent*
   direction input — live-shadow only (structural-fact LLM is already refuted).
6. **Keep the SELLER as the parallel +EV path** — it needs no direction call at all.

**Best single bet for tomorrow:** wire the new rolling direction model as a **confirmer inside an
abstaining agreement ensemble on big-move bars**, shadow it live (paper), and measure side-accuracy
on the *current* regime before it ever sizes a trade. Expect ~55–61% only on the big-move/agree
subset; ~coin flip elsewhere (so abstain there).

---

## 5. Current thresholds / algo (state as of 2026-06-15)

**Trade window:** 09:45–15:00 (entry + direction both).

**Entry / move-detection:**
- Rolling entry model (per-bar velocity from 09:45, 10-min, leak-free): served walk-forward 2024
  AUC ~0.78 (all-window), 0.72 morning. Marginal over the old 11:30 model (+0.009) — the morning
  "win" was look-ahead. Bundle on ML VM `~/entry_rolling_bundle.joblib`; **not deployed**.
- **Gate 1 = SELECTION, not elimination** (`strategy_app/engines/opportunity.py`):
  rank **relative to today** (percentile, or `score_cutoff` + multi-day baseline to kill early bias)
  + **cost floor** (`atr_14_1m * sqrt(hold_bars=10) ≥ ~108pt`) + **≤3/day budget** + 20-min spacing.
  Score blends **ATR percentile (primary, always-available) + rolling model prob (secondary, sharper)**.
  Built + tested (9 tests); **not yet wired** into the engine (replaces the absolute
  `VOL_GATE_ENTRY`/`ML_ENTRY` threshold).
- Legacy absolute gate (still live): `ATR_ENTRY_MIN_PCT=0.00088` (eliminator — gives 0 trades on
  quiet days; this is what selection replaces).

**Direction:** new rolling model (§3), used as a confirmer in an abstaining agreement ensemble (§4).
`recommended_min_prob=0.60`. `ML_ENTRY_DIRECTION_MODE` selects the live mode.

**Other gates (eliminators, keep):** time-window, regime-guard (`REGIME_GUARD_MAX_ORW`),
regime-tagger, trap-gate, confidence (`STRATEGY_MIN_CONFIDENCE`), strike-veto, policy, oversight.

**Cost:** ~1% round-trip measured (~108pt at BankNifty levels) — the floor every trade must clear.

**Real money:** OFF (paper). Buying not +EV (direction+cost); seller is the only proven +EV path.

---

## 6. June 2026 verification (selection + models, real recent data)

- **Rolling entry model ranks real June moves:** on 06-11 its top-2 prob bars were the 114pt &
  160pt moves (vs median 38pt). On quiet days (06-10/12) correctly low/flat.
- **ATR-rank ≈ model** for selection (corr 0.92); ATR even beat the model on 06-10 (found 89pt).
  ATR's edge: always-available, stable multi-day baseline → recommended as the selection backbone.
- **Percentile selection has early-session bias** → use `score_cutoff` + multi-day baseline.
- Quiet days → few/0 trades because moves don't clear the cost floor — the *correct* zero.

---

## 7. Resume checklist (tomorrow)

1. Read `~/train_direction.log` on ML VM → record direction accuracy (overall + big-move bucket +
   confidence-gated) into §3 above. Pull bundle if useful (acc on big-move/agree subset is the number).
2. Decide direction design: wire new model as confirmer in abstaining agreement ensemble (§4).
3. Wire the **selection Gate 1** into the engine (ATR+prob, score_cutoff+baseline) and SIM-validate.
4. Shadow direction live (paper) on the current regime before any sizing.
5. Keep seller validation as the parallel +EV track.
