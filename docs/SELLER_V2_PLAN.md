# Seller v2 — The Informed Seller (Plan of Record)

Born 2026-07-09 from the four-hat brainstorm (mathematician / trader / architect / PM)
after the June verification cycle concluded: buy-side has no harvestable edge at any
threshold; selling is the only historically net-positive system; account funding to
₹300k approved conditional on this architecture.

## The core insight (mathematician)

- TP 0.5×credit / stop 2×credit ⇒ breakeven win rate = 80%. The 2024 backtest's 78%
  only profited because real losses averaged < 2×. The strategy lives on a few points
  of win rate.
- Our entry model (dhan_entry_bundle_v3, AUC 0.68 — too weak to BUY with) is a
  calibrated QUIET detector: P(100pt move) ≤ 0.10 ⇒ market stays quiet 92.3% of bars
  (June holdout, n=1385, vs 67% base). Selling gamma inside that gate is the single
  highest-value hypothesis we own. Same model, zero new training, used on the side
  where its information lives.
- Sample budget: 8 months chain data ≈ 150+ backtest trades ⇒ detects ~10pt win-rate
  deltas; max ~3 tunable params per layer; every hypothesis PRE-REGISTERED before
  testing; walk-forward only.

## How sellers actually die (trader) — each gets an explicit defense

1. Event days (RBI/Fed/budget/elections): IV rich BECAUSE a move is coming.
   → event-calendar veto (no model substitutes).
2. Overnight gaps (BN monthly ⇒ 5-day hold = 4 gaps).
   → backtest MUST split intraday-theta vs overnight-carry P&L; carry only if the
   edge demonstrably lives there.
3. Gamma week near expiry. → DTE window selection (hypothesis: 15-25 DTE for BN).
4. Thin credit. → credit ≥ ~30% of wing width or skip.
Also: NIFTY weeklies as a second stream (sample rate + expiry diversification).
Exits only — NO adjustment engine (that's where retail sellers bleed).

## Non-negotiables (architect) — lessons paid for in real money 2026-07-09

- Backtest drives the REAL SellerRunner (injected clock + data provider). Never a
  parallel implementation: the ad-hoc buyer harness lied about thresholds, and the
  "proven" 2024 seller backtest hid a bug that made live entries impossible
  (2099-expiry placeholder, fixed 5c4ec0c).
- Startup reconciliation: seller book vs broker /positions must match or halt loudly.
- Entry latch + backoff (the 30s retry loop burned hedge-leg costs 3× today),
  margin PRE-check before leg 1, idempotent leg tags.
- Kill switches: daily 2% / weekly 5% caps, fast-onset tripwire exit, VIX halt.
- Probe-verify every new Dhan order type on a zero-risk far-OTM order first
  (STOP_LOSS_MARKET silently converts to instant LIMIT sell — real incident).
- Config contract pins for every new knob. Wings ARE the disaster stop: defined-risk
  structures need no resting SL and dodge the naked-short-margin trap entirely.

## Scope discipline (PM)

Reuse, don't build: quiet-gate = existing entry model; tripwire = existing fast
model; OI features already computed. v1 trains NO new ML. No LLM. No adjustments.
Capital ramps on evidence: 1 lot until 20 live trades match paper, regardless of
funding available.

## Phases & gates

| Phase | Deliverable | Gate (kill criterion if failed) | Est |
|---|---|---|---|
| 0 Safety | backoff latch; seller-first priority; startup reconciliation; margin pre-check | all deployed + contract-pinned | 1d |
| 1 Truth harness | shim drives real SellerRunner over Nov24–Jul26 chain data; baseline current rules; intraday/overnight split | baseline expectancy measured; harness reproduces the known paper trades | 2-3d |
| 2 Edge layers | pre-registered A/Bs: quiet-gate, event veto, credit floor, DTE window, tripwire, OI-wall strikes, NIFTY weeklies | keep walk-forward survivors only; combined ≥ +1.5%/mo on margin, maxDD < 10% | 3-4d |
| 3 Paper | both instruments, final config | 2 weeks inside backtest noise band | 2w |
| 4 Live ramp | 1 lot → 2 concurrent → scale (₹300k) | 20 live trades matching paper | — |

Buyer during all phases: ENTRY_ML_MIN_PROB=0.55, 1 lot, yields to seller — research
book, not P&L book.

## Current blockers carried in

- Seller container STOPPED (2026-07-09) pending Phase 0.
- Super Order stops (buyer leg protection without naked-short margin): deferred to
  after Phase 2 — seller wings don't need it; buyer at 0.55 rarely trades.
- NIFTY June snapshot backfill (for NIFTY-weekly backtests beyond Jun30): needs
  dhan_build_sim_snapshots + backfill into phase1_market_snapshots_nifty.
