# B-2.1 — ConflictAnalysis + OpportunityQuality design spec

**Date:** 2026-06-06 · **Author:** team CLAUDE · **Story:** B-2.1 (P0) · **Hand-to:** CURSOR for B-2.2 (`strategy_app/brain/decision_brain.py`)
**Companions:** [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md) §6 · [INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md](INTELLIGENT_BRAIN_IMPLEMENTATION_PLAN.md) §2 (D1, D4, D5) · [INTELLIGENT_BRAIN_SCRUM_BOARD.md](INTELLIGENT_BRAIN_SCRUM_BOARD.md)

> **Status note (board order):** this spec runs **ahead of its formal dependency** (B-1.5 sense review / B-1.x senses not built yet). It is written against the **sense contracts** (the `SenseVerdict` shapes), not their implementations, so it stays valid once CURSOR/CODEX build the senses. Treat the exact field names below as the **contract CODEX must satisfy** when building each sense — flag any divergence back here.

---

## 0. Where this sits

ConflictAnalysis and OpportunityQuality are **Layer-2 logic, not senses.** They are the two steps a sense cannot do itself because they **peek at every sense at once** (handover §4: "comparing senses is a Layer-2 job"). They live in `decision_brain.py`, consume the per-bar `dict[str, SenseVerdict]`, and feed the policy ladder (§4 below).

```
senses (Layer 1, parallel) ──► dict[str, SenseVerdict] ──► DecisionBrain (Layer 2)
                                                              ├─ ConflictAnalysis  → ConflictVerdict
                                                              ├─ OpportunityQuality → OpportunityVerdict
                                                              └─ policy ladder → BrainDecision{action, side, size=1, trace}
```

**Two invariants inherited from the doctrine (D1):** size is **always 1 lot** (selectivity is the only lever); **every bar writes a trace** (trade and no-trade).

---

## 1. Input contract — the SenseVerdicts this logic reads

Each sense returns `SenseVerdict{verdict, confidence, evidence: dict, value}` (B-1.0). This spec reads the following **evidence keys**. These are the binding field names for the sense builders:

| Sense | `verdict` domain | evidence keys consumed here |
|---|---|---|
| `regime` (IntradayRegime) | `alive` / `compressed` / `expanding` / `dead` / `chaotic` | — (only `verdict`) |
| `move` (MoveFunction) | `loaded` / `released` / `quiet` | `score:int`, `released:bool`, `expected_move_pt:float`, `prob_100:float`, `prob_200:float`, `horizon_min:int` |
| `direction` | `CE` / `PE` / `UNKNOWN` | `side:str`, `confidence:float\|None`, `basis:list[str]` |
| `destination` | `room` / `no_room` | `available_space_up:float`, `available_space_down:float`, `space_to_move_ratio:float`, `nearest_support:float`, `nearest_resistance:float` |
| `flow` (Flow/OFI) | `bull` / `bear` / `neutral` | `net_ofi:float`, `ce_bid_strength:float`, `pe_bid_strength:float` |
| `cost_ev` (Cost/EV) | `+ev` / `-ev` | `gross_if_right_pct:float`, `gross_if_wrong_pct:float` *(negative)*, `cost_pct:float`, `net_after_cost_pct:float` |
| `risk` | `ok` / `blocked` | `daily_dd:float`, `consec_losses:int`, `in_position:bool` |
| `execution` | `ok` / `degraded` | `spread_pct:float`, `liquidity:str` |

**Price-direction-of-move context** (needed by ConflictAnalysis case B): the `move` sense additionally exposes `evidence["last_bar_return"]` (signed points, the bar's close-vs-prev-close) so conflict logic can compare flow direction against price direction *without* importing another sense.

---

## 2. ConflictAnalysis (handover §6a)

> Contradictions are more informative than confirmations. **A conflict never produces a TRADE** — it forces `WAIT` (transient, re-check next bar) or `SKIP` (this opportunity is structurally unsound).

### 2.1 Output

```python
@dataclass(frozen=True)
class ConflictVerdict:
    any: bool                      # True if >=1 conflict fired
    conflicts: list[str]           # names of fired conflicts, e.g. ["ofi_bullish_price_falling"]
    action: str                    # "" if none; else the most severe of {"WAIT","SKIP"}
    evidence: dict[str, object]    # per-conflict trigger values, for the trace
```

`action` severity order: **`SKIP` > `WAIT`** (a structural conflict outranks a transient one). If multiple fire, take the max-severity action; record all names.

### 2.2 The four cases (exact triggers + action)

All thresholds are **named constants** (defaults below), tunable only via sim-gated change (D6 oversight), never auto-live.

| # | Conflict | Fires when | Action | Why |
|---|---|---|---|---|
| A | `move_strong_but_direction_conflicted` | `move.verdict in {loaded,released}` **AND** `direction.side != UNKNOWN` **AND** `flow.verdict != neutral` **AND** `flow` disagrees with `direction.side` (flow `bull` vs side `PE`, or `bear` vs `CE`) | **WAIT** | The spring is loaded but the two direction reads fight. Re-check next bar; it may resolve. |
| B | `ofi_bullish_price_falling` | `flow.verdict == bull` **AND** `move.evidence["last_bar_return"] < -RET_EPS` (or `flow bear` AND `last_bar_return > +RET_EPS`) | **WAIT** | Order flow and price are decoupled — absorption or a fake-out. Don't act mid-decouple. |
| C | `velocity_up_volume_weak` | `move.evidence` shows `velocity=True` **AND** `volume=False` (released-by-velocity-only) | **WAIT** | A price pop with no participation rarely sustains 10 min. Wait for volume to confirm. |
| D | `loaded_but_no_space` | `move.verdict in {loaded,released}` **AND** `destination.space_to_move_ratio < SPACE_MIN` | **SKIP** | The move is real but there's no room to the nearest wall — capped upside, full downside. Structurally unsound; skip the slot. |

**Defaults:** `RET_EPS = 5.0` pt · `SPACE_MIN = 1.0` (i.e. expected move must fit inside available space). Case D's `SPACE_MIN` is the **same gate** as the policy ladder's destination step (§4) — encoded once, here, so the ladder can stay a thin sequence.

> **Independence preserved:** ConflictAnalysis reads only `evidence` already produced by the senses. It never recomputes a sense's job; it only *compares* their verdicts. Case A/B compare direction vs flow; C reads move internals; D reads move vs destination.

### 2.3 Worked examples

- **A fires:** `move=loaded(score3)`, `direction=CE(0.58)`, `flow=bear(net_ofi −0.4)` → `bear` vs `CE` disagree → `{any:True, conflicts:["move_strong_but_direction_conflicted"], action:"WAIT"}`. Brain → WAIT.
- **D fires:** `move=released, expected_move_pt=135`, `destination.space_to_move_ratio=0.59` (resistance 80 pt away, move needs 135) → `0.59 < 1.0` → `action:"SKIP"`. Brain → SKIP (no slot spent).
- **None fire:** `move=released`, `direction=CE(0.62)`, `flow=bull`, `space_ratio=1.8`, `velocity=True/volume=True` → `{any:False, action:""}` → ladder proceeds to OpportunityQuality.

---

## 3. OpportunityQuality (handover §6b)

> Promoted from a sense to **the** gate. A signal is worth one of the day's few trades only if its net edge clears a threshold **and** it ranks among the best. **Used to rank and gate — never to size** (D1).

### 3.1 Division of labour (the key design decision)

The contested **option-premium physics** (how an `expected_move_pt` in the underlying maps to option-premium %, including delta/gamma/theta) lives **entirely in the Cost/EV sense** (B-1.4, wraps `cost_model.py`), which owns it and tests it. OpportunityQuality does **only decision composition**: mix the right/wrong outcomes by a *reference* direction probability, subtract nothing extra (cost is already in the sense), rank, and threshold. This keeps the disputed assumptions in one place with one set of tests, and makes the B-2.6 direction-accuracy sweep a one-line substitution (§5).

Cost/EV returns, for the bar's `expected_move_pt`:
- `gross_if_right_pct` — premium % gained on a correct-side 10-min hold (≈ +4% near a 100 pt move; the sense calibrates `k_up`).
- `gross_if_wrong_pct` — premium % lost on a wrong-side hold, **already capped by the live exit floor** (`EXIT_MAX_LOSS_PCT` / scalper stop) so it reflects the real stopped loss, not the raw adverse move (negative, e.g. −5%).
- `cost_pct` — round-trip brokerage + charges + slippage from `cost_model.py` (≈ 1.3%), expressed as % of premium.

### 3.2 Edge formula (direction-accuracy explicit)

```
net_pct(p) = p * gross_if_right_pct  +  (1 - p) * gross_if_wrong_pct  -  cost_pct
```
- `p` = assumed probability the direction call is correct.
- **At decision time** the brain gates on `p = P_REF` (the realistic *structural-bias* reference, default **0.55** — the documented CE-bias / abstain regime, NOT 0.50 and NOT the optimistic model AUC). This is the "realistic structural-bias direction" of Acceptance D5.
- **Edge** used for ranking/threshold: `edge = net_pct(P_REF)`.

`gross_if_wrong_pct` is negative, so `net_pct` is **linear and increasing in `p`** — exactly the curve B-2.6 reports.

### 3.3 Quality rank 0..10

A bar that clears the edge threshold is then **ranked** so the brain takes only the day's best slots (handover: ~8 move-signals/day → 2–4 trades). Rank blends three normalized, independent merits:

```
quality = round(10 * (W_EDGE * n(edge) + W_TAIL * n(prob_200) + W_ROOM * n(space_to_move_ratio)))
```
- `n(x)` = min-max normalization to [0,1] using fixed reference scales (NOT cross-bar, so a bar's score is stable and traceable):
  `n(edge)=clamp(edge / EDGE_FULL)`, `n(prob_200)=clamp(prob_200 / 0.20)`, `n(space)=clamp((space_ratio-1)/ (SPACE_FULL-1))`.
- **Weights:** `W_EDGE=0.5, W_TAIL=0.3, W_ROOM=0.2` (edge dominates; the 200-pt tail is the asymmetric-payoff source per D1; room breaks ties).
- **Reference scales (defaults):** `EDGE_FULL = 0.03` (3% net = a 10/10 on the edge axis), `SPACE_FULL = 3.0`.

### 3.4 Output + gate

```python
@dataclass(frozen=True)
class OpportunityVerdict:
    edge_pct: float          # net_pct(P_REF)
    quality: int             # 0..10
    p_ref: float             # the reference direction prob used
    net_curve: dict[float, float]   # {0.50:.., 0.55:.., 0.58:.., 0.60:.., 1.0:..} for the trace + B-2.6
    passes: bool             # edge_pct > EDGE_THRESHOLD and quality >= QUALITY_MIN
    evidence: dict[str, object]
```
**Gate:** `passes = (edge_pct > EDGE_THRESHOLD) and (quality >= QUALITY_MIN)`.
**Defaults:** `EDGE_THRESHOLD = 0.0` (strictly net-positive at `P_REF`) · `QUALITY_MIN = 5`.

> The brain **always populates `net_curve`** (cheap; it's the same linear formula at 5 points). That single field is what B-2.6 pastes as the sensitivity curve and what makes the D5 acceptance evaluable per-trade, not just in aggregate.

---

## 4. The policy ladder (the §6 sequence, exact)

`DecisionBrain.decide(senses) -> BrainDecision`. Evaluated top-down; first match wins:

```
0. risk.verdict == blocked            -> SKIP   (reason: risk_<daily_dd|consec|in_position>)
1. regime.verdict not in {alive,expanding}
                                      -> NO_TRADE (reason: regime_<state>)
2. move.verdict == quiet OR move.score < SCORE_MIN
                                      -> NO_TRADE (reason: no_loaded_spring)
   # NOTE: per Phase-0 B-0.2, the gate is the `loaded` PAIR, not the sum-of-4 score.
   #       SCORE_MIN is a floor on corroboration, not the additive score (which is RETIRED).
   #       `move.verdict==loaded` already encodes compression AND oi_build.
3. conflict.any                       -> conflict.action  (WAIT or SKIP; reason: conflicts[])
4. direction.side == UNKNOWN          -> WAIT  (reason: direction_unknown)
   # low-confidence CE/PE maps to UNKNOWN at the sense boundary (D5) — the brain trusts side.
5. destination.space_to_move_ratio < SPACE_MIN
                                      -> SKIP  (reason: no_room)   # redundant w/ conflict D; kept explicit
6. NOT opportunity.passes             -> SKIP  (reason: edge_<value> | quality_<value>)
7. execution.verdict == degraded      -> SKIP  (reason: spread_<value>)   # don't trade into bad fills
8. else                               -> TRADE side=direction.side, size=1
ALWAYS                                -> write trace (all verdicts + ladder branch + net_curve)
```

`SCORE_MIN` default `3` (kept for forward-compat / a future graded `loaded`), but **step 2 passes on `move.verdict==loaded` regardless** — the verdict is the source of truth, not the legacy additive score.

```python
@dataclass(frozen=True)
class BrainDecision:
    action: str          # "TRADE" | "WAIT" | "SKIP" | "NO_TRADE"
    side: str            # "CE" | "PE" | ""  (only set when action=="TRADE")
    size: int            # ALWAYS 1 when TRADE, else 0  (D1 — asserted in B-2.3 test)
    reason: str          # the first-match reason code above
    ladder_step: int     # which rung decided (0..8) — for the trace
    verdicts: dict        # snapshot of all SenseVerdicts + ConflictVerdict + OpportunityVerdict
```

---

## 5. Hook for B-2.6 (the cost-aware e2e GO/NO-GO)

The whole spec is shaped so B-2.6 is mechanical:

1. **Costing** is already inside `cost_ev` (D4 — no 6 bps anywhere; `cost_model.py` only). The brain never re-costs.
2. **Direction-accuracy sensitivity curve** = aggregate the per-trade `OpportunityVerdict.net_curve` across all TRADE decisions for each `p ∈ {0.50, 0.55, 0.58, 0.60, 1.0}`. Because `net_pct(p)` is linear in `p`, summing per-trade curves = the portfolio curve. **No re-simulation per accuracy point.**
3. **D5 acceptance becomes a direct read:** PASS if `portfolio_net(P_REF=0.55) ≥ 0` **OR** the curve crosses zero at an achievable accuracy (≤ ~0.60) with `portfolio_net(1.0) > 0`. **STOP only if `portfolio_net(1.0) < 0`** — i.e. unprofitable even with perfect direction (then move/destination/cost is the problem, not direction).
4. **Latency assertion (D6):** the ladder is pure arithmetic over a dict — assert `decide()` p99 < 1s with no LLM import on the path.

---

## 6. Constants table (single source — CURSOR puts these in `decision_brain.py`)

| Name | Default | Owner of tuning | Notes |
|---|---|---|---|
| `RET_EPS` | 5.0 pt | oversight (sim-gated) | conflict B price/flow decouple band |
| `SPACE_MIN` | 1.0 | oversight | conflict D + ladder step 5 |
| `SCORE_MIN` | 3 | oversight | forward-compat floor; verdict==loaded is authoritative |
| `P_REF` | 0.55 | **data (Sprint 4)** | realistic structural-bias direction; replace with the direction sense's measured accuracy once it ships |
| `EDGE_THRESHOLD` | 0.0 | oversight | strictly net-positive at P_REF |
| `QUALITY_MIN` | 5 | oversight | selectivity strength |
| `W_EDGE/W_TAIL/W_ROOM` | 0.5/0.3/0.2 | oversight | rank blend |
| `EDGE_FULL` / `SPACE_FULL` | 0.03 / 3.0 | oversight | rank normalization scales |

---

## 7. Open questions for B-2.2 / B-1.4 (flag, don't silently decide)

1. **Cost/EV premium mapping — PARTIALLY RESOLVED (still the #1 e2e error source).** `cost_ev` now uses **empirical anchors** (handover §1: right ≈ +4%, wrong ≈ −7.5%, the exit-giveback asymmetry), not a symmetric guess or an exit-blind delta model. The asymmetry is the point: on the synthetic e2e it puts break-even at ~0.70 direction accuracy. **Still pending real per-fill calibration** — the anchors are live-trading averages, not fitted to these 7 days. The `mfe_capture` lever lets B-2.6 model Phase-4 exit improvements (raising right-side → lower break-even).
2. **`P_REF = 0.55` provenance:** is the structural CE-bias 0.55 on the accrued 7 days, or aspirational? Tie it to a measured number before B-2.6 reports, else the curve's "realistic" point is unanchored. (CLAUDE B-3.3 will own this once the direction sense exists.)
3. **WAIT vs SKIP accounting in B-2.6 — RESOLVED.** The runner enforces a 10-min in-position cooldown (via the Risk sense reading `in_position`), so a loaded window that trades cannot be re-counted within the horizon.

---

## 8. Acceptance (this story)

- [x] All four ConflictAnalysis cases specified with exact triggers, actions, and worked examples.
- [x] OpportunityQuality edge formula + 0..10 rank + threshold, grounded in `cost_model.py` via the Cost/EV sense.
- [x] Full policy ladder with reason codes and the size=1 invariant.
- [x] Direction-accuracy sensitivity made mechanical for B-2.6 (linear `net_curve`).
- [ ] **Architect sign-off** + handoff to CURSOR (B-2.2). _record here_
