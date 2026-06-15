# Direction Combination Search ("the tree") — findings

*2026-06-14. Systematic search over direction signals, scored on bars where a move
actually came, split early-days vs late-days for robustness. Harness:
`c:/tmp/direction_tree.py` (+ sweep), data `c:/tmp/dir_bars.json` (8 full days from
mongo). Signals: vwap (`price_vs_vwap`), mom5/mom15 (`fut_return_5m/15m`), ema
(`ema_9-ema_21`), rsi (`rsi_14_5m`), macd (`macd_hist_5m`), pcr, oi (`ce_pe_oi_diff`),
maxpain (`distance_to_max_pain_pct`).*

## The two questions, separated (your framing)

1. **"Is a move coming?" — SELECTION, low threshold.** Detect likely movement
   (ATR / opportunity score), don't eliminate. This is the opportunity gate.
2. **"Which way?" — only matters once a move is coming.** Search combinations.

## What the search found

Scored on move-bars (|forward 10–15m return| ≥ threshold), pooled + early/late split:

| Move size | sample | Direction skill |
|---|---|---|
| ~80 pt (0.15%) | 545 | **coin-flip & inverts**: best single pcr 52% pooled but 61% early → 47% late; best robust combo ~50% |
| ~108 pt (0.20%) | 322 | ~52%, marginal |
| **~160 pt (0.30%)** | 83–181 | **vwap+pcr (±macd/mom5) ~59–62% in BOTH halves** |

**Conclusions:**
- On small/medium moves there is **no robust direction edge** — every signal/combo
  that looks good pooled is carried by early days and **inverts** later
  (non-stationary). Reproduces the 2024/2026 OOS-inversion finding cleanly.
- `momentum_15m`, `macd`, `maxpain` are weak/anti on small moves; `ema` weak.
- The **only** place a possibly-robust signal appears is **big moves (≥~160pt)**,
  via **vwap + pcr agreement** (~59–62%, both halves). Matches the prior 2024
  "big-move lever." **Caveat:** thin sample (83–181 bars over 8 days); the combo
  was chosen by search (selection bias). Treat as a hypothesis to validate on more
  data, not a proven edge.

## The resulting decision tree (evidence-based)

```
1. Move coming?  (opportunity score / ATR — LOW threshold, selection)
   ├─ no  → NO TRADE
   └─ yes → 2. How big is the expected move?
            ├─ small/medium (<~160pt)  → STRADDLE        (direction unreliable)
            └─ big (>=~160pt)          → 3. vwap AND pcr agree on a side?
                                          ├─ agree    → directional CE / PE   (~59% edge, thin)
                                          └─ disagree → STRADDLE
```

This is exactly "direction = how to trade, not whether": default **straddle**
(direction-agnostic, captures the move), and only take a **directional CE/PE** bet
in the one regime where direction shows a signal — **big moves with vwap+pcr
agreement**. Everything else stays straddle.

## How to re-run / extend the tree

`python c:/tmp/direction_tree.py` — change `MOVE_THR` / `H`, add signals to `SIGS`,
or add rules (the search already does singles, pair-agreement, k-of-N quorum, and a
robustness split). When more days of data exist, re-run and confirm whether the
big-move vwap+pcr edge survives — that is the single most valuable thing to retest.

## UPDATE — big-move conviction-gated ensemble (2026-06-14, the right design)

Refined per the operator's framing: **(1) ML gates the BIG move (high threshold);
(2) direction via a conviction-gated expert ensemble** — each member is a domain
expert that VOTES ONLY WHEN CONFIDENT (its feature is strongly directional);
**act on confident agreement, VETO when divided, else abstain→straddle.**

On big-move bars (|fwd 12m| ≥ 0.30%, n≈139), confident-unanimous rule:

| Members (conviction-gated) | acc | cov | early / late |
|---|---|---|---|
| vwap + OR-break + straddle-expansion *(clean, natural sign)* | **59–61%** | 70–83% | 62 / 58 ✅ |
| + skew member | 67% | 58% | 80 / 67 ⚠️ **overfit** (skew "confident" 99% = constant flipped tilt) |
| plain majority-of-confident (no veto) | 62% | 84% | **52** / 65 ✗ not robust |

**Findings:**
- **Conviction-gating + veto beats majority** (majority early=52% ≈ coin-flip; veto
  early=62–75%). Acting only on confident agreement is the unlock — exactly the
  operator's "if a member is 100% sure, go; if divided, veto" intuition.
- The **clean, non-overfit edge is ~60% on big moves** from **vwap + opening-range
  break + directional straddle expansion** (`atm_ce_return_1m` vs `atm_pe_return_1m`).
  These are the NEW families that work — NOT PCR/OI (exhausted, confirmed).
- Coverage ~70–83%: on the rest, members divide → **straddle** (direction-agnostic).
- Caveat: n≈100–139 big-move bars over 7 days. Robust across the split but thin;
  needs more data. The skew-driven 67% is overfit — use ~60%.

**Data gaps that would most improve direction (per the tiers):**
- **DEPTH microstructure — NOT collected** (`DEPTH_FEED_INSTRUMENTS` empty; the
  collector has been sleeping). Highest-value unexplored signal; turn it on
  (set instrument tokens + restart depth_collector) to backtest it going forward.
- **Relative strength (Nifty)** — not collected (BankNifty only).

Harnesses: `c:/tmp/bigmove_direction.py`, `c:/tmp/ensemble_conviction.py`,
data `c:/tmp/rich_bars.json`.

## Honest bottom line
The buy-side is **magnitude-strong, direction-weak**. The search confirms you
cannot manufacture a robust direction from these signals on small moves. The
disciplined design is: **select on move-detection, trade straddle by default, tilt
directional only on big-move vwap+pcr agreement (and keep validating that)**.
