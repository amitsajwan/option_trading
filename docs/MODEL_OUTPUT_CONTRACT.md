# Model Output Contract

> **As-of:** 2026-05-19 · **Owner:** strategy_app/engines/pure_ml_staged_runtime.py
>
> What the prediction layer emits for each snapshot, what every field
> means, and how the runtime consumes it.

For where these fields surface in JSONL files, see [`OBSERVABILITY_GUIDE.md`](OBSERVABILITY_GUIDE.md).
For the gate chain that wraps the model, see [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md).

---

## The contract object: `StagedRuntimeDecision`

Defined in [`strategy_app/engines/pure_ml_staged_runtime.py:39-63`](../strategy_app/engines/pure_ml_staged_runtime.py).
This is what BOTH the legacy 3-stage path (`predict_staged`) AND the
multi-bundle path (`select_best_bundle_decision`) return for every
snapshot the engine evaluates.

```python
@dataclass(frozen=True)
class StagedRuntimeDecision:
    action: str                                  # "HOLD" | "BUY_CE" | "BUY_PE"
    reason: str                                  # short reason code (used in logs)
    entry_prob: float = 0.0                      # Stage-1 / bundle probability
    direction_up_prob: float = 0.0               # Stage-2 directional probability
    ce_prob: float = 0.0                         # = direction_up_prob (CE bias)
    pe_prob: float = 0.0                         # = 1.0 - direction_up_prob (PE bias)
    recipe_id: Optional[str] = None              # "ATM_PE_9", "ATM_CE_15", ...
    recipe_prob: float = 0.0                     # winning bundle's prob (multi-bundle)
    recipe_margin: float = 0.0                   # margin = recipe_prob - bundle.threshold
    horizon_minutes: Optional[int] = None        # max_hold_bars from recipe
    stop_loss_pct: Optional[float] = None        # stop pct (decimal, of premium)
    target_pct: Optional[float] = None           # target pct (decimal, of premium)
    risk_basis: str = "option_premium"           # how stops/targets are interpreted
    model_diagnostics: dict[str, Any] = ...      # per-stage internal scores + reasons
    selected_strike: Optional[int] = None        # pre-picked strike (bundle path)
    selected_strike_reason: Optional[str] = None # "bundle_atm" / "bundle_atm_offset_+1"
```

---

## Field-by-field meaning

### Action + reason

| Field | Values | What it means |
|---|---|---|
| `action` | `"HOLD"`, `"BUY_CE"`, `"BUY_PE"` | The decision. HOLD means no new entry this minute. |
| `reason` | short code | Why this decision. For HOLD, identifies which stage rejected. For BUY_*, conventionally `"recipe_selected"` (3-stage) or `"option_pnl_fire"` (bundle). |

Common HOLD `reason` codes:
- `entry_below_threshold` — Stage 1 `entry_prob` below `stage1.selected_threshold`
- `direction_below_threshold` — Stage 2 both CE and PE prob below their thresholds
- `direction_low_edge_conflict` — Stage 2 prob conflict, `|ce - pe| < min_edge`
- `stage2_feature_incomplete` — too many NaNs in Stage 2 features
- `missing_atm_strike` — chain ATM lookup failed
- `option_pnl_hold:prob_below_threshold:0.5234` — bundle path; prob fell short

### Probabilities

| Field | Range | What it means |
|---|---|---|
| `entry_prob` | 0-1 | Stage-1 / bundle prediction of "is this a profitable trade?" Probability that the recipe will hit target before stop/max-hold. |
| `direction_up_prob` | 0-1 | Stage-2 prediction "will futures move up?" Used for CE vs PE decision in 3-stage path. |
| `ce_prob`, `pe_prob` | 0-1, sum ≈ 1 | Side-specific probabilities. `ce_prob = direction_up_prob`. |
| `recipe_prob` | 0-1 | For multi-bundle: the winning bundle's own prob output. |
| `recipe_margin` | -1 to 1 | `recipe_prob - bundle.decision_threshold`. How much above the bar the model was. |

A `recipe_margin = 0.07` means the model was 7 percentage points above the
firing threshold — comfortable conviction. `recipe_margin = 0.001` means
"barely above the gate" — likely high-variance.

### Recipe metadata

| Field | What it is |
|---|---|
| `recipe_id` | The recipe being executed: `"ATM_PE_9"`, `"OTM1_CE_15"`, etc. Sets the strike offset + side + max-hold. |
| `horizon_minutes` | Max bars to hold = recipe's max_hold_bars (5, 9, 15). |
| `stop_loss_pct` | Decimal. `0.25` = exit when premium drops 25% from entry. |
| `target_pct` | Decimal. `0.40` = exit when premium rises 40% from entry. |
| `risk_basis` | `"option_premium"` (stops/targets are pct of premium) or `"underlying"` (legacy C1 staged; pct of futures). |

### Strike pre-selection (bundle path only)

| Field | When present | What it means |
|---|---|---|
| `selected_strike` | Multi-bundle path only | The strike the bundle picked. Engine MUST use this (no smart-strike override). |
| `selected_strike_reason` | with `selected_strike` | `"bundle_atm"` or `"bundle_atm_offset_+1"` etc. — audit trail of how the bundle picked. |

When `selected_strike` is set, [`pure_ml_engine.evaluate`](../strategy_app/engines/pure_ml_engine.py)
bypasses smart-strike entirely. This is required for labeler-runtime
equivalence (the labeler used a fixed offset; runtime must match).

### Diagnostics

`model_diagnostics` is a dict that varies per path. In the 3-stage path it
contains per-stage `input_diagnostics` (n features used, NaN counts) and
per-stage `output_prob`. Useful for debugging "why did the model see weird
features?" — typically referenced after a misfire to confirm features
weren't degraded.

---

## How the runtime consumes the contract

[`PureMLEngine.evaluate`](../strategy_app/engines/pure_ml_engine.py) treats the
returned `StagedRuntimeDecision` as input for a series of subsequent
gates (see [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md) gates 7-11).

The chain that consumes the decision:

```python
decision = predict_staged(...) | select_best_bundle_decision(...)

if decision.action == "HOLD":
    log HOLD with decision.reason; return None

# decision is BUY_CE / BUY_PE
if decision.selected_strike is None:
    # bundle path expected a strike — abort
    log HOLD; return None

# Build the trade signal
signal = TradeSignal(
    signal_type=ENTRY,
    direction=decision.action[-2:],          # CE | PE
    strike=decision.selected_strike,
    stop_loss_pct=decision.stop_loss_pct,
    target_pct=decision.target_pct,
    max_hold_bars=decision.horizon_minutes,
    confidence=max(decision.ce_prob, decision.pe_prob),
    decision_metrics={
        "entry_prob": decision.entry_prob,
        "direction_up_prob": decision.direction_up_prob,
        "recipe_prob": decision.recipe_prob,
        "recipe_margin": decision.recipe_margin,
        ...
    },
    reason="ml_pure_staged: action=BUY_X entry_prob=... recipe=... risk_basis=...",
)
```

The `reason` string format above is human-readable AND machine-parseable —
the dashboard's date-classifier and the audit harness both grep it for
`recipe=NAME` and `prob=N.NNN`.

---

## Where you see these fields in JSONL streams

| Field | `signals.jsonl` | `positions.jsonl` | `decision_traces.jsonl` |
|---|:---:|:---:|:---:|
| `action` | yes (as `signal_type`) | yes (as `event`) | yes |
| `reason` | yes (`reason` text) | yes (as `entry_reason` on POSITION_OPEN) | yes |
| `entry_prob` | yes (`decision_metrics.entry_prob`) | yes (POSITION_OPEN `decision_metrics`) | yes |
| `recipe_id` | yes (in `reason` text + `decision_metrics`) | yes (in `entry_reason` text) | yes |
| `recipe_margin` | yes | yes | yes |
| `selected_strike` | yes (`strike` field) | yes (POSITION_OPEN `strike`) | yes |
| `model_diagnostics` | partial | partial | yes (full) |

For "which JSONL do I grep for X?", see [`OBSERVABILITY_GUIDE.md`](OBSERVABILITY_GUIDE.md).

---

## Stability guarantees

This contract is stable across both prediction paths (3-stage staged and
option-PnL multi-bundle). When new bundle types are added or new model
families introduced, they MUST return a `StagedRuntimeDecision` with the
same field names — the runtime, dashboard, audit harness, and operator
scripts all depend on this stability.

Adding new fields is OK as long as they have sensible defaults; removing
or renaming existing fields is a breaking change requiring synchronized
updates to the engine, the persistence consumer, and the dashboard
historical-replay classifier.
