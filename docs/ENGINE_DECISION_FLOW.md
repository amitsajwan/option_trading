# Engine Decision Flow — AUTHORITATIVE (start here)

> **As-of:** 2026-06-09 · **Owner:** strategy_app
>
> **What this doc is:** the single, plain-English source of truth for how the
> live engine turns a market snapshot into a trade (or a "do nothing"). Every
> claim links to the exact code line. If another doc disagrees with this one
> about the *live* path, this one is right (see §7).

---

## 1. The 30-second mental model

Every minute, a **snapshot** of the market arrives. The engine asks a series of
yes/no questions ("gates"). **If any gate says no, it does nothing (HOLD).**
If all gates pass, it places **one** trade. That's it.

```
snapshot ──► [common gates] ──► pick a DIRECTION (CE/PE) ──► [per-trade gates] ──► TRADE
                  │                      │                          │
                  └── any fail ──────────┴──────────────────────────┴──► HOLD (do nothing)
```

Two models help, at different steps:
- **Entry model** = "is a big move likely?" (a *gate*).
- **Direction model** = "which way, call or put?" (picks the *side*).

Exits are decided **separately** by the exit policy — the models have no say once a trade is open (§6).

---

## 2. Which engine is actually live (this trips everyone up)

The repo contains **multiple engines**. Only one runs live. Confirm it from the
startup log (`docker logs <strategy_app> | grep "starting engine"`):

| Setting | Live value | Meaning |
|---|---|---|
| `engine=` | **`deterministic`** | file [`deterministic_rule_engine.py`](../strategy_app/engines/deterministic_rule_engine.py) — **this is the one** |
| `strategy_profile_id=` | **`trader_master_live_v1`** | the active profile (decides which code path runs — see §4) |
| `pipeline_v2=` | **`False`** | the "v2" entry pipeline is OFF |
| `ML_ENTRY_DIRECTION_MODE` (env) | **`consensus`** | how the direction is chosen (§5) |

> ⚠️ **`PureMLEngine`** ([`pure_ml_engine.py`](../strategy_app/engines/pure_ml_engine.py)),
> documented in [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md), is **NOT live.**
> Reading that doc to understand live behavior sends you down the wrong path.

---

## 3. Follow one snapshot, step by step

All of this happens in **[`_process_entry_votes()` — deterministic_rule_engine.py:818](../strategy_app/engines/deterministic_rule_engine.py#L818)**.

**Step A — common gates** (apply to *every* profile; each one can stop the trade):

| # | Gate | Code | Stops the trade when… |
|---|---|---|---|
| 1 | Time window | [:818](../strategy_app/engines/deterministic_rule_engine.py#L818) (`is_in_configured_time_window`) | outside `ENTRY_TIME_WINDOWS` |
| 2 | **Regime guard** | [:833–845](../strategy_app/engines/deterministic_rule_engine.py#L833) | `REGIME_GUARD_MAX_ORW>0` **and** the day's opening range is wider than that (expansion/event day). *Off by default.* |
| 3 | Regime tagger | (`ENTRY_REGIME_TAGGER`) | the session's regime tag isn't allowed |
| 4 | Trap gate | (`ENTRY_TRAP_GATE_ENABLED`) | too few "trap" cues present |

**Step B — choose the direction-resolution path** (the part that confused us — see §4).

**Step C — per-candidate gates** ([:1093–1106](../strategy_app/engines/deterministic_rule_engine.py#L1093)), in order:

`confidence_gate` ([:1115](../strategy_app/engines/deterministic_rule_engine.py#L1115), vote confidence < `STRATEGY_MIN_CONFIDENCE`) →
`strike_vetoed` → `policy_gate` → `oversight_veto` → **`_build_entry_signal()` = the trade fires.**

---

## 4. The direction dispatch — three paths, only ONE is live

After the common gates, the engine picks **how** to resolve CE-vs-PE. There are
three branches ([dispatch starts at :1042](../strategy_app/engines/deterministic_rule_engine.py#L1042)):

```
deterministic_rule_engine.py:1042
│
├─ (1) if STRATEGY_ENTRY_PIPELINE_V2 == 1     ──► _process_entry_votes_v2()  [:1263]
│         └─ uses resolve_direction_consensus ONLY if profile ∈ _PROFILES_ML_ENTRY_CONSENSUS
│         (LIVE: pipeline_v2 = False → this branch is SKIPPED)
│
├─ (2) elif profile ∈ _PROFILES_ML_ENTRY_CONSENSUS   ──► _process_entry_consensus()  [:1125]
│         └─ this set = { trader_master_ml_entry_consensus_v1 } ONLY  [see :104]
│         └─ THIS is the only path that calls resolve_direction_consensus()
│         (LIVE: our profile is trader_master_live_v1, NOT in the set → SKIPPED)
│
└─ (3) else   ◄══════════  trader_master_live_v1 LANDS HERE  [:1054–1123]
          └─ direction = each vote's OWN .direction
             (that direction was set earlier in ml_entry.py — see §5)
          └─ resolve_direction_consensus() is NEVER called on this path.
```

**Plain English:** our live profile takes branch **(3)**. The direction was
*already decided* inside the entry model's vote; this path just picks the
best-scoring vote and applies the per-trade gates.

- Profile-set definition: [`_PROFILES_ML_ENTRY_CONSENSUS` — :104](../strategy_app/engines/deterministic_rule_engine.py#L104)
- The branch our profile hits: [:1054–1123](../strategy_app/engines/deterministic_rule_engine.py#L1054)

---

## 5. Where the live direction is *actually* chosen

In **[`ml_entry.py` :243](../strategy_app/engines/strategies/ml_entry.py#L243)**, when
`ML_ENTRY_DIRECTION_MODE=consensus`:

```python
hint_dir, hint_source = _resolve_direction(snap)   # ml_entry.py:244  ← the side is chosen HERE
... ml_direction_ce_prob = <direction model prob>  # :256  (often None — see note)
direction = hint_dir                               # :260
```

- The side-picker: [`_resolve_direction()` — ml_entry.py:103](../strategy_app/engines/strategies/ml_entry.py#L103).
- `composite` mode is a *different branch* of the same function — that's why the
  A/B (composite 44% vs consensus 59%) is real: it flips this branch.
- **`ml_direction_ce_prob` is often `None`** (the direction-only model only fills
  it when `DIRECTION_ML_MODEL_PATH` is set *and* it returns a prob) — this is the
  "ce_prob coverage gap" that makes a confidence gate hard.

---

## 6. Exits (totally separate from entry/direction)

Once a trade is open, the **exit policy stack** decides when to close —
the entry/direction models have **zero** say. The built stack is logged at
startup as `exit policy mode=...`. Live = `scalper, max_loss_floor=10%,
hard_stop_7%, thesis_fail_5b, trail, premium_target`. Code: [`position/exit_policy.py`](../strategy_app/position/exit_policy.py).

---

## 7. ⚠️ Dead-code traps — where edits do NOTHING live

This is what cost us hours. For the **live profile**, these are dead:

| If you edit… | …it does | Because |
|---|---|---|
| [`resolve_direction_consensus()` — direction_consensus.py:40](../strategy_app/engines/direction_consensus.py#L40) | **nothing live** | only called on path (2), which our profile skips |
| env `DIRECTION_CONSENSUS_MIN_MARGIN`, `DIRECTION_ML_CONFIDENCE_MIN` | **nothing live** | only read inside that dead function |
| anything gated on `is_consensus` in [`entry_pipeline_gates.py`](../strategy_app/engines/entry_pipeline_gates.py) | **nothing live** | v2 pipeline is OFF |

**To change LIVE direction behavior, edit one of these two places only:**
1. [`_process_entry_votes()` :818](../strategy_app/engines/deterministic_rule_engine.py#L818) (a common gate — e.g. the regime guard at :833), or
2. [`_resolve_direction()` :103](../strategy_app/engines/strategies/ml_entry.py#L103) (the side-picker).

### Which env overrides actually work live
| Override | Works live? | Read at |
|---|---|---|
| `ML_ENTRY_DIRECTION_MODE` | ✅ | [ml_entry.py:243](../strategy_app/engines/strategies/ml_entry.py#L243) |
| `STRATEGY_MIN_CONFIDENCE` | ✅ | [confidence_gate :1115](../strategy_app/engines/deterministic_rule_engine.py#L1115) |
| `REGIME_GUARD_MAX_ORW` | ✅ (since 2026-06-09) | [common gate :833](../strategy_app/engines/deterministic_rule_engine.py#L833) |
| `DIRECTION_CONSENSUS_MIN_MARGIN`, `DIRECTION_ML_CONFIDENCE_MIN` | ❌ dead for live profile | dead function only |

---

## 8. How to test a change WITHOUT guessing

Use the sim and **compare `trade_count` / `win`, never `overrides_applied`**:
```
POST /api/ops/sim/today  {"date":"2026-06-03","overrides":{"REGIME_GUARD_MAX_ORW":"0.005", ...}}
GET  /api/ops/sim/<job_id>   → read summary.trade_count / win_count
```
> **`overrides_applied: True` only means the sim set the env var — NOT that the
> engine honored it.** A lever is only "working" if it *changes the numbers*.
> (Example: `REGIME_GUARD_MAX_ORW=0.005` on Jun 3 took trades 7 → 0. That's proof it fires.)

---

## 9. Which docs to trust
| Doc | Trust for |
|---|---|
| **THIS doc** | ✅ the live engine decision flow (authoritative) |
| [`RUNTIME_DECISION_FLOW.md`](RUNTIME_DECISION_FLOW.md) | ⚠️ `PureMLEngine` only — **not live** |
| `SIGNAL_TO_TRADE_FLOW.md`, `ENTRY_AND_DIRECTION.md`, `SYSTEM_FLOW_DIAGRAMS.md` | ⚠️ older/partial — defer to this doc on any conflict |
| `DIRECTION_PANEL_V1_SPEC.md` | the *plan* for direction work — but its lever code must target §7's live path |

---

## 10. Glossary (plain English)
- **Snapshot** — a once-a-minute picture of the option chain + futures.
- **Gate** — a yes/no check; any "no" = HOLD (no trade).
- **Vote** — a strategy's suggestion `{direction, confidence}`. `ML_ENTRY` is the main one.
- **Profile** (`trader_master_live_v1`) — a named config that decides *which code path* runs.
- **Consensus vs composite** — two ways `_resolve_direction` picks the side (env `ML_ENTRY_DIRECTION_MODE`).
- **ORW** (`opening_range_width_pct`) — how wide the first-15-min range was, as a fraction (0.008 = 0.8%). Wide = "expansion/event day".
- **Regime guard** — skip trading on expansion days (where direction goes random).
