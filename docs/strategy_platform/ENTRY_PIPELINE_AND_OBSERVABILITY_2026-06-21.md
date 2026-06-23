# Entry Pipeline, Cost Gate & Observability — 2026-06-21

> Written after the 2026-06-20/21 work session. This is the **authoritative**
> description of the live entry decision flow, the new cost-ratio gate, the
> feature-health observability layer, the depth feed, and the ML-threshold
> rationale. If another doc disagrees about the live entry path, this one wins.

---

## TL;DR (the decisions made this session)

1. **ML threshold stays 0.35 as a SELECTION FLOOR** — not raised to 0.50. The model's
   base rate is 6.2%, so 0.35 ≈ 6× base = a strong magnitude signal. It is well
   calibrated (ECE 0.008). Raising it just removes count bluntly.
2. **Added a cost-ratio gate (arm B)** after the ML floor — direction-agnostic,
   removes setups whose expected move can't clear ~1.3% all-in cost. Fail-safe.
3. **VALIDATION FINDING (important):** on all available SIM data the cost gate is a
   **no-op** — every ML-fired bar already has a big enough expected move. The losing
   days lost on **DIRECTION** (wrong side, stopped out), which no cost gate can fix.
   Direction remains the wall, exactly as the long-run research says.
4. **Fixed a silently-dead VIX direction signal** (was reading the wrong snapshot key).
5. **Built a feature-health layer** — one command + every trace now says what data is
   flowing and what's missing. It caught the VIX-key and depth gaps.
6. **Depth feed**: infra already exists end-to-end; it's just not fed
   (`DEPTH_FEED_INSTRUMENTS` empty). Hardened to warn loudly + documented for tomorrow.

---

## 1. The live entry decision flow (as deployed 2026-06-21)

```
Every 1-min bar:
  [0] REGIME       CHOP/AVOID/PANIC → [] (no strategies)   else → [IV_FILTER, ML_ENTRY]
  [1] IV_FILTER    iv_percentile > 95% → SKIP
  [2] ML FLOOR     entry_compression_v1.predict(bar) ≥ 0.35   ← SELECTION (magnitude)
                     < 0.35 → no vote
  [3] COST GATE    expected_move_pt / all-in-cost ≥ 1.5       ← NEW (arm B), can only REMOVE
                     < 1.5 → no vote   (fail-safe: missing inputs → pass)
  [4] DIRECTION    multi_signal 6-signal stateless scorer:
                     ORB ±2, VWAP ±2, straddle ±2, PCR ±1, VIX ±1.5, EMA ±1
                     |score| < 2.0 → ABSTAIN (veto, can only REMOVE)
                     ≥2 → CE   ≤-2 → PE
  [5] EXECUTE      buy ATM, 1 lot (paper)
  [6] EXIT         adaptive: TREND/TRENDING/BREAKOUT → lottery (20% stop)
                            SIDEWAYS/other → scalper (3% tgt, 7% stop)
```

**Design principle — gates SUBTRACT, never ADD.** The ML model is the magnitude
predictor (our strongest signal, AUC 0.82). The cost gate and direction can only
*remove* trades; they can never rescue a weak-magnitude bar. We never dilute the
signal we're good at (magnitude) to lean on the one we're bad at (direction).

---

## 2. Why ML threshold = 0.35 is a floor, not a weak gate

From the model bundle (`entry_compression_v1.joblib`):

| metric | value |
|---|---|
| holdout AUC | 0.824 |
| **base rate** (big move ≥0.20% in 5 bars) | **0.062** (6.2%) |
| ECE raw / calibrated | 0.0098 / 0.0078 (excellent) |
| recommended_min_prob | 0.40 |

The neutral point is **6.2%, not 50%.** A prob of 0.35 ≈ 6× base rate = strong. The
model rarely emits high probs because big moves are rare (only 4 of ~24k holdout bars
exceeded 0.80) — so a 0.75 threshold would mean **zero trades, ever**. Round human
numbers are meaningless for a rare-event classifier.

Separation table (precision vs trades/day):

| threshold | precision | trades/day |
|---|---|---|
| 0.35 (live floor) | ~48% | ~5 |
| 0.40 (bundle rec) | 53% | 4.2 |
| 0.50 | 62% | ~2 |

There is a **quantization gap** (zero bars between 0.46 and 0.63), so 0.50/0.55/0.60
are the same operating point. We keep 0.35 as the *floor* and let the cost gate +
direction do the selectivity (decision: arm B over a blunt 0.50 raise).

---

## 3. The cost-ratio gate (`entry_cost_gate.py`)

**Question it answers:** "if we're right, does the move clear cost?" Direction-agnostic.

```
expected_move_pt = atr_ratio × spot × sqrt(hold_bars)      # per-bar, varies with vol
gain_if_right_%  = right_slope × expected_move_pt           # CostEvSense empirical anchor
all-in cost_%    = brokerage(cost_model) + slippage_placeholder
cost_ratio       = gain_if_right_% / cost_%
  ratio < 1.5 → DROP
```

**Env knobs** (all live-tunable, gate is on by default):

| env | default | meaning |
|---|---|---|
| `ENTRY_COST_RATIO_GATE_ENABLED` | 1 | master switch (set 0 to disable) |
| `ENTRY_COST_RATIO_MIN` | 1.5 | gain must be ≥1.5× all-in cost |
| `ENTRY_COST_HOLD_BARS` | 10 | hold horizon for the sqrt scale |
| `ENTRY_COST_SLIPPAGE_PCT` | 0.008 | **placeholder** until depth measures real bid-ask |

**FAIL-SAFE:** missing `atr_ratio`/`spot`/`premium` → gate PASSES (never silently
blocks). Every entry's `entry_model.cost_gate` trace records expected_move, ratio,
and the keep/drop decision.

### ⚠ Validation finding — the gate is currently a NO-OP

Replaying every historical SIM entry (May 27 / Jun 2 / Jun 18) through the real gate:
**all kept** (expected moves 68–210pt, ratios 1.9–6.4, all clear cost). The losing
days lost on direction:

```
Jun 18 (−16%):  09:48 CE → STOP_LOSS −8.35%   (wrong side)
                10:40 PE → STOP_LOSS −7.84%   (wrong side)
                both prob 0.076/0.122 → ALSO below the 0.35 ML floor (floor removes them)
```

So on current data the **ML floor does the filtering; the cost gate adds nothing**;
residual losses are **direction**. We keep the gate enabled because it is fail-safe,
adds per-bar EV observability to traces, and **will** start biting once the depth feed
gives a real (likely higher) slippage number than the 0.8% placeholder. But it is not
a P&L improvement today — **direction is the real remaining lever.**

---

## 4. Feature-health observability (`strategy_app/diagnostics/feature_health.py`)

One source of truth for "what data is flowing." Two consumers:

- **CLI**: the `LIVE FEATURE HEALTH` section in `ops/gcp/verify_config.py` reads the
  latest Mongo snapshot and prints a 21-required-feature board in seconds.
- **Every decision trace** now carries a compact `feature_health` block
  (`required_present/total`, `missing_required`, `degraded`) — so a trace ALONE tells
  you whether that decision ran on full or degraded inputs.

Required features (21): futures OHLCV, returns, vwap, ema_stack, atr,
compression_score, adx_14, vol_spike_ratio, opening_range, pcr, pcr_change_5m,
max_pain, total_oi, total_volume, atm_premium/oi/volume/iv, strike_chain, vix_current,
vix_intraday_chg. Optional (do NOT degrade the system): option_depth_bid/ask.

This layer **caught two real bugs this session**: the dead VIX key and the empty
depth feed.

---

## 5. The VIX direction-signal bug (fixed)

`multi_signal` read `futures_derived.vix_intraday_chg` — but that field lives in
`vix_context.vix_intraday_chg`. It was always None → the ±1.5 VIX weight **never
fired**. Fixed to read `vix_context` (with a legacy fallback). One of the six
direction signals is now actually contributing.

---

## 6. Depth feed — status & tomorrow (2026-06-23)

The whole pipeline already exists: `depth_collector.py` (Kite 5-level → Redis+Mongo),
`RedisDepthReader` (fail-safe), `DepthContext`, plugin, gate. It is **enabled**
(`DEPTH_FEED_ENABLED=1`) but **not fed**: `DEPTH_FEED_INSTRUMENTS` is empty in the
running ingestion container.

Two issues to fix before depth flows:

1. **Env not reaching the container** — `.env.compose` has the value but the running
   ingestion sees it empty (recreate without `--env-file`, or compose passthrough).
2. **Stale symbols** — the configured `...57800CE/PE` + `26JUN` are wrong: ATM is
   ~57000 and June expiry rolls 26 Jun. **These change daily with spot + expiry** and
   currently must be set by hand (a dynamic ATM resolver is the real fix — follow-up).

Fail-safe behaviour (verified): empty instruments → collector logs a **WARNING every
~10 min** + sleeps; reader returns None on absent/stale/malformed; cost gate falls
back to the flat slippage placeholder. **Depth being down never breaks trading.**

When depth flows: replace `ENTRY_COST_SLIPPAGE_PCT` with the measured half-spread, and
`feature_health` optional `option_depth_*` flips to ✓.

---

## 6b. SIM == LIVE parity (enforced 2026-06-22)

**Principle: the SIM must behave identically to live by default.** It was not —
the `strategy_app_sim` compose service was missing **46 decision-affecting env vars**
that `strategy_app` had (STRATEGY_MIN_CONFIDENCE, all RISK_*, all STRATEGY_STRIKE_*,
EXIT_MAX_LOSS_PCT, ML_ENTRY_*, DEPTH_*, …). SIM silently used **code defaults** for
those → different strike selection, risk sizing, confidence gating, exits than live.
(That, plus the SIM running a stale image, was why a SIM result never matched live.)

**Fixed:** mirrored every decision var from `strategy_app` into the
`strategy_app_sim` env block + aligned the one mismatched default
(`OPTION_PNL_MODEL_BUNDLE`). Verified by dumping the **actual** env of a spawned SIM
container vs the live container:

```
live=162  sim=172   decision-var diffs (excl. legitimate isolation): 0
```

**Legitimately different (SIM isolation — must NOT match):**
`MONGO_COLL_*` (writes to `*_sim` collections), Redis `*_TOPIC`/`STREAM_NAME`,
`STRATEGY_RUN_DIR`, `SIM_RUN_ID`, `STRATEGY_CONSUMER_*`, `STRATEGY_ROLLOUT_STAGE`,
and **`MARKET_SESSION_ENABLED`** (live=1 / sim=0 — SIM replays history, must not run
the live market-session scheduler).

**Guarded:** [`test_sim_live_parity.py`](../../strategy_app/tests/test_sim_live_parity.py)
parses both compose env blocks and fails CI if SIM ever misses a decision key or
uses a different default. SIM=live is now a structural invariant, not luck.

**Build parity:** both services build from the same context + `strategy_app/Dockerfile`
→ identical code. Rebuild BOTH together (`docker compose build strategy_app
strategy_app_sim`) so they never run different code (the no-docker-cp rule).

## 7. Deployment state (FINAL — 2026-06-22)

Deployed via a **proper image rebuild** (the docker-cp interim was superseded — see
the no-docker-cp rule). `docker compose --env-file .env.compose build strategy_app
strategy_app_sim`, then recreated `strategy_app` (`up -d --force-recreate --no-deps
--env-file`). Verified: container healthy, `rollout_stage=paper`, exit
`adaptive[lottery=BREAKOUT,TREND,TRENDING]`, no errors; `execution_app` untouched
(adapter=dhan, safety unchanged).

**LIVE + SIM (in the rebuilt images):**
- cost-ratio gate (`entry_cost_gate.py`) + wiring in `ml_entry.py`
- VIX-key fix + max_pain/OI/cross-family direction signals (`entry_direction_policy.py`)
- `feature_health` on every trace (`deterministic_rule_engine.py` + `diagnostics/`)
- SIM↔live env parity in `docker-compose.gcp.yml` (env-only, no rebuild needed)

**Staged in repo, deploys on NEXT rebuild (behavior-neutral / minor):**
- `ingestion_app/collectors/depth_collector.py` — empty-instrument WARNING
- `market_data_dashboard/routes/sim_routes.py` — whitelist new SIM env-overrides
- `deterministic_rule_engine.py` — wiring of `SIDEWAYS_RETURNS_MIXED_GATE_ENABLED`
  (**DECISION REQUIRED — see below**)

### ⚠ Decision required: SIDEWAYS_RETURNS_MIXED_GATE_ENABLED
This env var was **dead** — the SIDEWAYS+returns_mixed entry block at
`deterministic_rule_engine.py:998` was hardcoded and ignored the var, even though
`.env.compose` (and 6 sim scripts) set it to `0`. The code is now wired to honor it
(default `true`), **but not yet deployed.** On the next rebuild:
- `.env.compose=0` → the SIDEWAYS+returns_mixed block turns **OFF** (more SIDEWAYS
  entries — which is what the stale config asked for, but it never took effect).
- set `=1` → block stays **ON** (current protective behavior; SIDEWAYS+mixed is the
  losing regime per research).

Decide before the next strategy_app rebuild. Until then, live behavior is unchanged
(block ON, hardcoded).

**Permanent rule:** always rebuild BOTH `strategy_app` and `strategy_app_sim` from the
branch — never docker-cp. SIM runs image code, so cp'd changes are untested in SIM.

---

## 8. Direction work — max_pain + OI + cross-family agreement (built 2026-06-21)

Direction is the wall. Added the OOS-grounded positioning signals that were missing
from `multi_signal`, plus cross-family agreement. All env-gated and coarse-weighted
(n is tiny — avoid overfit).

### New signals (added to the 6-signal scorer)

| # | Signal | Weight | Bullish (CE) | Bearish (PE) | Env toggle |
|---|---|---|---|---|---|
| 7 | **max_pain pin** | ±1 | spot below pin (pulled up) | spot above pin | `ENTRY_MS_MAXPAIN_ENABLED` (1) |
| 8 | **OI walls** | ±1 | near PE wall (support) | near CE wall (resistance) | `ENTRY_MS_OIWALL_ENABLED` (1) |

max_pain ignores moves <0.1% (already pinned = noise). OI wall compares distance from
spot to the top CE-OI strike (resistance) vs top PE-OI strike (support). Both are
**independent confirmers** — noise standalone (~50%) but the memory edge is
*agreement* of vwap + max_pain + OI on big moves (~60%).

### Cross-family agreement

Signals are grouped into orthogonal families and each family's net lean is computed:

| Family | Signals |
|---|---|
| price_action | ORB, VWAP, EMA |
| options_flow | straddle, PCR, max_pain, OI walls |
| volatility | VIX |

`ms_families_agree` (0–3) = how many families point the winning way — recorded on every
vote's `raw_signals`. **Optional gate** `ENTRY_MS_MIN_FAMILIES` (default 0 = off):
require ≥N families to agree before trading. This is the anti-overfit lever — reward
agreement across *independent* families, not a raw sum of correlated price signals.

### VIX fix folded in

Signal #5 (VIX) now reads `vix_context.vix_intraday_chg` (was the dead `futures_derived`
key) — so for the first time all 8 signals can contribute.

### Validation discipline

These ship **disabled-capable and unproven**. Before `ENTRY_MS_MIN_FAMILIES` earns a
nonzero live value, A/B in SIM across multiple days + both 2024 halves. Every prior
direction "edge" that wasn't walk-forward-validated turned out to be coin-flip selection
(quorum 50.3%, momentum anti-signal, clean-move AUC 0.49). Keep weights coarse.

## 9. What's next

1. SIM A/B the new direction signals + `ENTRY_MS_MIN_FAMILIES` ≥2 across dates.
2. Add a **regime-inversion penalty** — direction is non-stationary; penalize a side
   that has been losing recently.
3. Replace the flat slippage placeholder with depth-measured half-spread once the feed
   is fed (set `DEPTH_FEED_INSTRUMENTS` to today's ATM CE/PE).
