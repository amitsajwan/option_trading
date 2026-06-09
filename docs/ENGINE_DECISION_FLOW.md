# Engine Decision Flow — AUTHORITATIVE

> **As-of:** 2026-06-09 · **Owner:** strategy_app
>
> **The single source of truth for how the LIVE engine turns a snapshot into a trade.**
> Verified by tracing the actual dispatch, not by reading older docs (several of
> which describe engines/paths that are NOT live — see §6).

---

## 0. The one fact that confused everyone

There are **three different direction-resolution code paths**, gated by a
combination of a flag, a profile-membership set, and an env var. **Only ONE is
live**, and it is **not** the obvious one. Most of last night's "lever" sweeps
measured nothing because the lever code sat on a path the live profile never takes.

**The live engine is `deterministic_rule_engine.py`. The live profile is
`trader_master_live_v1`. Its direction comes from `ml_entry.py:_resolve_direction`
— NOT from `resolve_direction_consensus`.**

---

## 1. Which engine + profile is live

| Thing | Value | Where to confirm |
|---|---|---|
| Engine | `deterministic` (`deterministic_rule_engine.py`) | startup log `engine=deterministic` |
| Profile | `trader_master_live_v1` | startup log `strategy_profile_id=...` |
| Direction mode | `consensus` (env `ML_ENTRY_DIRECTION_MODE`) | `printenv ML_ENTRY_DIRECTION_MODE` |
| Entry pipeline v2 | **OFF** (`STRATEGY_ENTRY_PIPELINE_V2=0`) | startup log `pipeline_v2=False` |

> `PureMLEngine` (`pure_ml_engine.py`, documented in `RUNTIME_DECISION_FLOW.md`)
> is a **different, non-live** engine. Ignore it for live behavior.

---

## 2. Per-snapshot entry flow (`_process_entry_votes`, deterministic_rule_engine.py:818)

Runs every minute. Early **common gates** (apply to ALL profiles), each `return None` = abstain:

1. **Time window** — `is_in_configured_time_window` (`ENTRY_TIME_WINDOWS`)
2. **Regime guard** — `REGIME_GUARD_MAX_ORW` → abstain if `snap.opening_range_width_pct >= threshold` (expansion/event days). *Off by default.* **← lives here on purpose (see §4).**
3. **Regime tagger** — `ENTRY_REGIME_TAGGER`
4. **Trap gate** — `ENTRY_TRAP_GATE_ENABLED`

Then the **direction-resolution dispatch** (the crux):

```
deterministic_rule_engine.py:1026
├─ if STRATEGY_ENTRY_PIPELINE_V2 == 1:        → _process_entry_votes_v2()      [v2 gate cascade]
│                                                uses resolve_direction_consensus ONLY if
│                                                profile ∈ _PROFILES_ML_ENTRY_CONSENSUS
│
├─ elif profile ∈ _PROFILES_ML_ENTRY_CONSENSUS:  → _process_entry_consensus()  [legacy consensus]
│      (set = { trader_master_ml_entry_consensus_v1 } ONLY)
│                                                uses resolve_direction_consensus + respects its veto
│
└─ else:   ◄────────────  trader_master_live_v1 LANDS HERE  (lines 1042–1107)
           direction = each vote's OWN .direction (set by ml_entry.py:_resolve_direction)
           picked among ranked votes; ML-policy resolves CE/PE conflicts.
           resolve_direction_consensus is NEVER CALLED.
```

After a direction + candidate survive, the **per-candidate gates** (lines 1093–1106):
`confidence_gate` (`< STRATEGY_MIN_CONFIDENCE`) → `strike_vetoed` → `policy_gate` → `oversight_veto` → **`_build_entry_signal`** (the trade).

---

## 3. Where direction actually comes from (live path)

`ml_entry.py` builds the `ML_ENTRY` vote and sets its `.direction`:

- **`ML_ENTRY_DIRECTION_MODE=consensus`** (`ml_entry.py:243`): `direction = _resolve_direction(snap)` (hint), and tries to attach `ml_direction_ce_prob` from the direction-only model — **but only if `DIRECTION_ML_MODEL_PATH` is set and the model returns a prob** (often `None` → the `ce_prob` coverage gap).
- **`composite`** (`ml_entry.py:261+`): different `_resolve_direction` branch.

The A/B (composite 44% vs consensus 59%) is real **because it flips `_resolve_direction`'s branch** — that code IS on the live path.

---

## 4. Dead-code traps (where lever code does NOTHING for the live profile)

| Code | Reads env | Live effect | Why |
|---|---|---|---|
| `resolve_direction_consensus` (`direction_consensus.py`) | `DIRECTION_CONSENSUS_MIN_MARGIN`, `DIRECTION_ML_CONFIDENCE_MIN`, the regime guard I first put here | **NONE** | function not called for `trader_master_live_v1` |
| Anything gated on `is_consensus` in `entry_pipeline_gates.py` | — | **NONE** | v2 pipeline is OFF + profile not in the consensus set |

**Rule of thumb:** to change LIVE direction behavior for `trader_master_live_v1`,
the code must be in **`_process_entry_votes` (common gates)** or in
**`ml_entry.py:_resolve_direction`** — nowhere else.

### Env overrides: which actually take effect live
| Override | Works live? | Read at |
|---|---|---|
| `ML_ENTRY_DIRECTION_MODE` | ✅ | `ml_entry.py` |
| `STRATEGY_MIN_CONFIDENCE` | ✅ | confidence_gate (`:1098`) + replay_engine |
| `REGIME_GUARD_MAX_ORW` | ✅ (since 2026-06-09, relocated to `_process_entry_votes`) | `:~833` |
| `DIRECTION_CONSENSUS_MIN_MARGIN` / `DIRECTION_ML_CONFIDENCE_MIN` | ❌ dead | only `resolve_direction_consensus` |

---

## 5. Exits (unchanged, separate from entry/direction)
Once open, the **exit policy stack** (`position/exit_policy.py`) decides — entry/direction
models have no say. Built stack logged at startup as `exit policy mode=...`. Live:
`scalper, max_loss_floor=10%, hard_stop_7%, thesis_fail_5b, trail, premium_target`.

---

## 6. Doc status (stop trusting the stale ones)
| Doc | Status |
|---|---|
| **THIS doc** | ✅ authoritative for live engine decision flow |
| `RUNTIME_DECISION_FLOW.md` | ⚠️ describes `PureMLEngine` (NOT live) — keep for that engine only |
| `SIGNAL_TO_TRADE_FLOW.md`, `ENTRY_AND_DIRECTION.md`, `SYSTEM_FLOW_DIAGRAMS.md` | ⚠️ partial/older — defer to this doc on any conflict about the live path |
| `DIRECTION_PANEL_V1_SPEC.md` | design spec; note its lever code must target §4's live path, not `resolve_direction_consensus` |

## 7. How to verify any claim here yourself
```
# which engine/profile/pipeline is live:
docker logs <strategy_app> | grep -E 'engine=|strategy_profile_id=|pipeline_v2='
# does an override actually change behavior? (the only real test)
POST /api/ops/sim/today {overrides:{...}}  → compare trade_count/win, NOT overrides_applied
```
**`overrides_applied=True` only means the sim set the env — NOT that the engine honored it.**
Always confirm a lever by a *change in trade_count/win*, never by the applied flag.
