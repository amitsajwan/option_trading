# Design Spec: Entry Pipeline Refactor (Gate Cascade)

**Status:** Proposed — design only, no code yet.
**Goal:** Replace the three duplicated entry paths inside `DeterministicRuleEngine`
with **one** loosely-coupled cascade of gates. Make the system *better* and easier to
reason about — we explicitly do **not** need to bit-match current live behaviour.

**Non-goals:** Changing exit logic (Stage 8). Changing the ML models. Changing what a
"good trade" is. This is a structural refactor of *how decisions are sequenced and
traced*, not *what* the decision is (except where the old code was inconsistent — see
§7 Divergence).

---

## 1. Why (the problem in the current code)

The [Signal→Trade spec](SIGNAL_TO_TRADE_FLOW.md) describes **one** cascade. The code
implements it **three times** inside `evaluate()`:

| Path | Entry point | When it runs |
|---|---|---|
| Consensus / bypass | `_process_entry_consensus` (~L866) | ML_ENTRY vote ≥ bypass conf |
| Scored candidates | `ml_can_resolve_direction_conflict` loop (~L822) | ML can break a CE/PE conflict |
| Sequential ranked | ranked loop (~L854) | fallback |

Each re-implements *strike → policy → confidence → build* in a slightly different order,
checks vetoes in slightly different places, and communicates through stringly-typed
`vote.raw_signals["_strike_vetoed"]` / `["_policy_allowed"]` keys.

**Consequences (all observed in this repo's history):**
- A fix or knob must be applied in *N* paths; miss one → silent divergence.
- Vetoes log at different levels (`debug` vs `info`) or not at all (confidence is a bare
  `continue`). Debugging "why no trade?" means reading three code paths.
- Config is read via `os.getenv` *inside* the leaf logic (e.g. `select_strike`), so a
  knob that isn't wired (the original ₹500-cap bug) fails silently with no single place
  to assert it was bound.

## 2. The design — one cascade of gates

### 2.1 Contracts

```python
class GateOutcome(Enum):
    PASS = "pass"      # continue to next gate
    VETO = "veto"      # stop the pipeline, no trade
    SKIP_CANDIDATE = "skip_candidate"  # this vote is dead, try the next ranked vote

@dataclass(frozen=True)
class GateResult:
    outcome: GateOutcome
    reason: str = ""
    values: dict[str, Any] = field(default_factory=dict)  # numbers behind the decision

    @classmethod
    def ok(cls): ...
    @classmethod
    def veto(cls, reason, **values): ...
    @classmethod
    def skip(cls, reason, **values): ...

class Gate(Protocol):
    name: str
    def apply(self, ctx: "EntryContext") -> GateResult: ...
```

### 2.2 The shared, typed context (replaces raw_signals as the bus)

```python
@dataclass
class EntryContext:
    # inputs (immutable for the run)
    snap: SnapshotAccessor
    regime: RegimeSignal
    risk: RiskContext
    votes: list[StrategyVote]
    config: EntryConfig                 # resolved ONCE — see §3

    # progressively filled by gates
    candidate: StrategyVote | None = None
    direction: Direction | None = None
    strike: int | None = None
    premium: float | None = None
    lots: int | None = None

    # observability
    trace: list[GateTrace] = field(default_factory=list)
```

Gates read inputs + earlier gates' fields, and write their own. No `os.getenv`, no
magic dict keys. A gate that needs a value another gate didn't set is a *type/None*
error caught in tests, not a silent runtime miss.

### 2.3 The pipeline

```python
ENTRY_PIPELINE: list[Gate] = [
    HardGatesGate(),        # session phase, risk paused, regime allowed, time window, regime conf
    VotesGate(),            # at least one usable StrategyVote
    DirectionGate(),        # VETO POINT 1 — consensus / ML-authority resolution
    StrikeDepthGate(),      # VETO POINT 2 — IV reject, premium hard-cap, depth
    EntryPolicyGate(),      # EntryPolicyDecision.allowed
    ConfidenceGate(),       # >= config.min_confidence
]

def evaluate(snap) -> Optional[TradeSignal]:
    ctx = build_context(snap)
    for vote in rank(ctx.votes):          # candidate loop lives HERE, once
        ctx.reset_candidate(vote)
        result = run_chain(ctx)
        if result is PASS:
            return build_signal(ctx)
        # VETO → stop everything; SKIP_CANDIDATE → try next vote
        if result.outcome == GateOutcome.VETO:
            return None
    return None
```

The **candidate loop exists in exactly one place**. `VETO` kills the bar; `SKIP_CANDIDATE`
moves to the next vote. This single distinction replaces the three hand-rolled loops.

### 2.4 Principle encoded: ML is advisory, the engine is authority

`DirectionGate` consumes the ML hint as *one input* to `resolve_direction_consensus`
alongside rule votes + shadow score + regime. The gate — not the ML model — emits the
`VETO`. This makes your "ML timing is only a suggestion" rule a structural fact, not a
convention.

---

## 3. EntryConfig — resolve every knob once

A single dataclass built once per run from env (or the OPS override map), then frozen.
This is the structural cure for "knob not wired."

```python
@dataclass(frozen=True)
class EntryConfig:
    # gates
    min_confidence: float                 # STRATEGY_MIN_CONFIDENCE
    bypass_min_confidence: float          # CONSENSUS_BYPASS_MIN_CONFIDENCE
    regime_min_confidence: float = 0.60
    entry_time_windows: tuple[...] | None # ENTRY_TIME_WINDOWS (None = no restriction)
    regime_allowed_tags: frozenset[str]   # ENTRY_REGIME_ALLOWED_TAGS

    # direction
    sideways_min_margin: float            # DIRECTION_MIN_MARGIN_SIDEWAYS
    ml_direction_weight: float            # DIRECTION_ML_WEIGHT
    ml_block_pe: bool; ml_block_ce: bool  # ML_ENTRY_BLOCK_*

    # strike / depth
    strike_policy: str                    # STRATEGY_STRIKE_SELECTION_POLICY
    smart_strike_enabled: bool            # STRATEGY_SMART_STRIKE_ENABLED
    max_premium: float                    # SMART_STRIKE_MAX_PREMIUM (0 = no cap)
    hard_premium_cap: bool = True         # SMART_STRIKE_HARD_PREMIUM_CAP (now default on)
    max_otm_steps: int                    # STRATEGY_STRIKE_MAX_OTM_STEPS
    iv_reject_pctile: float               # SMART_STRIKE_IV_REJECT_PCTILE
    otm_tiers: tuple[TierConfig, ...]     # the per-tier conf/iv/regime/OI gates

    # risk
    max_session_trades: int; max_consecutive_losses: int
    max_lots_per_trade: int; capital: float; per_trade_pct: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EntryConfig": ...
```

**Assertion at run start** (the missing safety net): after building `EntryConfig`, log
the *effective* values and assert internal consistency (e.g. `0 <= min_confidence <= 1`,
`max_premium >= 0`). The OPS SIM DIAG line should print these so the UI can never again
show "cap ₹500" while the engine runs ₹1100.

---

## 4. Gate responsibilities (one-liners)

| Gate | PASS when | VETO / SKIP |
|---|---|---|
| `HardGatesGate` | valid phase, not paused, regime+time allowed, regime conf ≥ min | VETO (environmental — whole bar dead) |
| `VotesGate` | ≥1 usable vote | VETO (nothing to act on) |
| `DirectionGate` | consensus resolves a direction | VETO if `consensus.vetoed`/ambiguous |
| `StrikeDepthGate` | affordable, priced, liquid strike found | VETO if IV reject / no strike under hard cap |
| `EntryPolicyGate` | `EntryPolicyDecision.allowed` | SKIP_CANDIDATE (another vote may pass) |
| `ConfidenceGate` | `candidate.confidence ≥ min_confidence` | SKIP_CANDIDATE |

Note the deliberate VETO-vs-SKIP split: environmental/direction failures kill the bar;
per-candidate policy/confidence failures just advance to the next vote. This is the
behaviour the three old loops *tried* to express, made explicit.

---

## 5. Observability — one trace to rule them all

Every gate result is appended to `ctx.trace`. On VETO/SKIP, emit one structured line:

```
entry_gate stage=StrikeDepth outcome=veto reason=rejected_premium_cap
           dir=CE atm=53800 max_premium=500 atm_ltp=1056 vote_conf=0.82
```

`build_context` also stamps a per-bar `decision_id` so the full cascade for one snapshot
is greppable. This directly serves §5 of the tech spec ("when a trade should have
happened but didn't").

---

## 6. Migration plan (strangler — never a big-bang rewrite)

1. **Land the contracts + EntryConfig** (`from_env`, fully unit-tested) — no engine wiring.
2. **Wrap existing logic into gates** that call the *current* helpers
   (`_evaluate_entry_policy`, `select_strike`, …). Behaviour-preserving by construction.
3. **Add the new `evaluate_v2()`** behind `STRATEGY_ENTRY_PIPELINE_V2=0` (default off).
4. **Golden-master harness:** replay N historical session days through BOTH `evaluate`
   and `evaluate_v2`; assert identical `(open?, dir, strike, premium, lots)` per bar.
   Log every divergence with the gate that caused it.
5. **Resolve divergences** as explicit decisions (§7), update golden master.
6. **Flip default to v2**, delete the three old paths in a follow-up once stable.
7. Full suite green at every step — it is the safety net.

## 7. Divergence policy ("better, not identical")

Because we don't need to match live, when v2 differs from v1 we classify each:
- **Bug-in-v1 fixed** (e.g. IV-reject that silently traded ATM) → keep v2, document.
- **Intentional tightening** (hard premium cap) → keep v2, document.
- **Accidental regression** → fix v2.
Every divergence is logged and reviewed; none ships silently.

## 8. Test plan

- **Per-gate unit tests** — pure `ctx → GateResult`, no engine boot. Fast, exhaustive.
- **Pipeline tests** — assert VETO stops the bar, SKIP advances to next vote, ordering.
- **EntryConfig.from_env tests** — every knob parsed; bad values rejected loudly.
- **Golden-master** — v1≡v2 on historical days (minus documented divergences).
- Existing 358 tests stay green throughout (they pin v1 until cutover).

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Core engine is safety-critical | Strangler + flag + golden master; old paths untouched until proven |
| Hidden coupling via `raw_signals` keys read elsewhere | Audit readers of `_strike_*`/`_policy_*`; keep writing them in v2 during transition |
| Behaviour drift no one notices | Mandatory divergence log; cutover only after review |
| Scope creep into exits/ML | Hard non-goal; gates stop at signal construction |

## 10. Open decisions for you

1. **Golden-master corpus:** which days/range to replay for v1≡v2 (e.g. last 60 sessions)?
2. **Cutover bar:** ship v2 as default once divergences are all "intended/fixed," or keep
   it flag-gated in live for a probation window first?
3. **Depth gate now or later:** fold the option-depth veto you mentioned into
   `StrikeDepthGate` in this refactor, or as a fast-follow once the cascade exists?
