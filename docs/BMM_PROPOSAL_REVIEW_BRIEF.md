# BMM Proposal — Review Brief (for the proposer)

> Purpose: a self-contained summary of our **current system**, what your proposal
> **already matches**, and the **results we already have** that bear on it — so we can
> verify the numbers together before building anything new.
> Date: 2026-06-16. Real money is OFF (paper). All numbers below are clean walk-forward
> /out-of-sample, not in-sample.

---

## 1. TL;DR — your proposal is ~80% already live

Your core thesis — **"detect the big move first (percent-based, multi-horizon), make
direction a separate downstream problem"** — is exactly the architecture we already run.
We split the system into two jobs deliberately:

| Your proposal | What we already have | Status |
|---|---|---|
| Big Move Model (BMM): "move ≥ X% in Y min?" | **`entry_only_v3`**: label = **0.20% move in 5 min, direction-agnostic** | ✅ live-capable, AUC **0.831**, calibrated (ECE 0.009) |
| Multi-horizon (0.10/0.20/0.30%) | We trained **0.10% (AUC 0.821), 0.13% (0.824), 0.20% (0.831)** | ✅ exist; v3 (0.20%) is the deployed one |
| Direction as Stage-2, conditional, fallback to straddle | Direction council runs **only after** the move/entry fires; abstain → straddle | ✅ live (`_regime_council_direction`) |
| Add compression/energy features (BB width, range ratio, EMA spread, dist-from-VWAP) | We tested this directly as the **E1 compression harness** | ✅ ran on 1199 days; results in §3 |
| Strict gates: freeze 2020–23, test once on 2024, forward-check 2026 | Same protocol used in E1 (walk-forward) and live SIM replay on June 2026 | ✅ |

So the question isn't "should we build a BMM" — **we have one, and it works (AUC 0.83).**
The real question your proposal raises is: **do the new compression/energy features make
the existing BMM materially better, and does the Stage-2 direction problem become solvable
on the BMM-positive subset?** We have early data on both. See §3 and §4.

---

## 2. Our current system (one screen)

Every minute a snapshot arrives (price, OI, IV, depth, VWAP). The engine asks, in order:

1. **Regime** — chop/event day → don't trade.
2. **Move detector (this is the BMM)** — `entry_only_v3` prob ≥ thr **OR** ATR ratio ≥ thr.
   - Honest finding: the ML move-detector (served AUC ~0.78–0.83) is **≈ ATR ratio**
     (correlation 0.92). Move detection was **never the bottleneck**.
3. **Selection Gate** — rank the bar **relative to today**, require it clear a **~108 pt
   cost floor** (= ATR·√10, the round-trip option cost), budget ≤3 trades/day.
4. **Direction council** — vwap + PCR + straddle (+ optional dir-model, + max_pain advisory);
   bet directionally **only in a trend**, require ≥2 agree, else **abstain → straddle/skip**.
5. **Safety gates** (confidence/strike/policy/oversight) → trade.
6. **Exit stack** (separate): scalper · 10% max-loss · 7% hard-stop · 5-bar thesis-fail ·
   trail · target · REGIME_SHIFT.

**The wall is not entry. The wall is direction + cost.** We can reliably tell *a move is
coming*; we cannot reliably tell *which way*, and the round-trip cost (~1%, ~108 pt) eats the
edge of a coin-flip direction.

---

## 3. RESULTS YOU CAN VERIFY — E1 compression harness

This is the experiment that directly tests your "compression / stored-energy → big move"
idea. Harness: `research/compression_harness.py`. Data: `snapshots_ml_flat_v2`, **1199
trading days, 2020-01 → 2024-10, ~378k in-window 1-min bars**, fully causal (completed bars
only — we caught and removed a look-ahead bug that had inflated AUC ~0.10).

**Setup definition:** compression (BB-width < trailing avg, ATR < trailing avg, range_10 <
0.6·range_30, EMA9/20 spacing tight → score ≥3) **followed by** an acceptance breakout
(close beyond prior-10-bar range **and** next bar holds). Then measure the forward 10-bar
move vs the all-bars base rate (= "lift").

### Move detection (does compression precede a big move?)
| Slice | ≥100pt base | ≥100pt after setup | **lift** | ≥150pt lift |
|---|---|---|---|---|
| **Walk-forward TEST (2024, OOS)** | 14.3% | 18.1% | **1.26** | **1.31** |
| 2024 Q1 / Q2 / Q3 / Q4 | — | — | 1.22 / 1.29 / 1.28 / **1.31** | 1.22 / 1.22 / 1.54 / 1.47 |
| Walk-forward TRAIN (2020–2023) | 12.2% | 12.9% | 1.06 | 1.04 |

- **Verdict: real but modest.** Compression→acceptance precedes a ≥100pt move ~26–31% more
  often than baseline in 2024, **stable across all four quarters**, and **TEST (1.26) > TRAIN
  (1.06)** → not overfit. But it's **below the 1.5× bar** we set for "this is a strong
  standalone trigger." It corroborates the existing BMM rather than beating it.
- Note lift **rises over time** (2020 ~0.8 → 2023 up to 1.9 → 2024 steady 1.2–1.3), i.e. the
  effect is regime-dependent and stronger in the recent regime.

### Direction (the critical finding)
The breakout **direction** (did price continue the way it broke out?) was:
| Slice | direction accuracy |
|---|---|
| 2024 TEST | **41.3%** |
| All quarters | 38–50% (mostly low-40s) |

- **Verdict: the breakout direction is ANTI-predictive.** Following the breakout = ~41%
  right → **fading the breakout ≈ 59%.** This contradicts the classic "trade the breakout
  direction" intuition but **matches every other direction study we've run** in this regime:
  flow-following (VWAP/EMA align) = 43% on big moves recently; fade-VWAP = 57%; LLM
  structural picker = 57% ceiling. BankNifty's recent regime **mean-reverts**.

**This is the headline for you to verify with us:** in 2020–2024 (and live 2026), the
*move* is detectable but the *follow-through direction* is not — if anything it inverts.

---

## 4. What we've already refuted (so we don't re-run it)

These are dead ends with new-information caveats — please push back if you think any deserve
another look, but we have clean evidence against each:

- **Relabeling entry→direction** (clean-move / monotonic-3-bar continuation): AUC collapses
  **0.83 → 0.49**. The direction coin-flip is *embedded* in the label.
- **Plain direction ML** (`direction_only_v2`): best-ever AUC 0.593 in-sample, **inverts to
  43.9% live OOS in 2026.** Non-stationary.
- **3-signal agreement lever** (vwap+OI+PCR agree on big moves): ~61% on 2024 big moves
  (the one thing that ever cleared break-even) — but **thin (n~83–181) and inverts in 2026.**
- **Momentum continuation** (2–5 min): anti-predictive (~47%); confirmation makes it worse.
- The C1-style directional cascade you flagged: agreed — strong in-sample, weak/negative
  clean holdout, F1/B1 held, VOLATILE-only retrain got *worse*. Hit a ceiling.

The only consistently **+EV** thing we've found is **selling premium** (S3 iron-condor,
regime-conditional) — a non-directional structure, which is also where your "fallback to a
straddle if direction is unstable" instinct points.

---

## 5. What your proposal adds that we have NOT done (the real value)

Stripping out what's already built, here's the **net-new** in your proposal that's worth
testing:

1. **Multi-horizon move detection trained jointly** (5/10/20/30/45 min × 0.10/0.20/0.30%).
   We trained single-horizon (5 min). A longer horizon (20–30 min) may catch the *sustained*
   moves that actually pay after cost, vs the 5-min noise we currently fire on.
2. **`time_to_move` as a regression output** — we've never predicted *when*. Could gate out
   "move comes but too slowly to beat theta."
3. **The compression/energy features fed as raw values into the BMM** (not booleanized).
   E1 booleanized them into a score; your version (raw into xgboost/lightgbm) is a fair,
   untested variant and is the cleanest incremental experiment.
4. **`position_in_day_range` / session-context features** — partially present, not
   systematically tested as a feature group.

Everything here reuses the existing pipeline (LightGBM/xgboost, walk-forward harness,
2020–2024 parquet, frozen-threshold gates). No infra rebuild — agreed with your read.

---

## 6. The open questions for us to verify together

1. **Does adding raw compression/energy features lift the existing BMM's AUC** above 0.831,
   on the **same** 2024 walk-forward holdout? (E1 says the signal is real but modest — so
   the honest prior is "small lift, maybe none.")
2. **Does a longer horizon (20–30 min) BMM beat the 5-min one after cost** (~108pt floor)?
   This is the most promising untested lever.
3. **On the BMM-positive subset, is direction any more solvable than on all bars?** Our E1
   answer so far: **no — it's anti-predictive (fade ~59%).** If you expected
   continuation-on-the-subset, this is the key disagreement to resolve with the data.
4. **If direction stays unsolvable, do we accept the non-directional conclusion** (trade the
   move with a straddle, or sell premium) rather than keep hunting a directional edge?

---

## 7. How to reproduce our numbers

- E1 harness: `research/compression_harness.py --root ~/parquet_data` (ML VM
  `option-trading-ml-01`; full per-month log at `~/compression_harness.log`).
- Existing BMM: GCS `published_models/entry_only_v3/`, label 0.20%/5min, AUC 0.831.
- Charter with full team critique + experiment ladder (E1–E5): `docs/COMPRESSION_STATE_ENGINE_PLAN.md`.
- System map: `docs/SYSTEM_FLOW.md`; model index: `docs/MODELS_INDEX.md`.
</content>
</invoke>
