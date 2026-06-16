# Config Consolidation Plan — one YAML, one loader, SIM≡LIVE

*Status: IMPLEMENTED in code (2026-06-14). All 5 phases landed on
`feat/intelligent-brain`. The only remaining step is the verified live cutover on
the VM (§7) — gated on the parity check, reversible with one env var.*

## Implemented artifacts
- `ops/strategy_config.yml` — the single grouped source of truth.
- `strategy_app/config/registry.py` — the one declaration table (60 keys).
- `strategy_app/config/loader.py` — `resolve()` + `apply_to_environ(env_wins|yaml_wins)`.
- `strategy_app/config/typed.py` — typed accessor `view()/value()` (Phase 5).
- `strategy_app/main.py` — loads config at startup (default `yaml_wins`); `ops_env.json`
  keys now derived from the registry (hand list deleted).
- `market_data_dashboard/routes/ops_routes.py` — SIM baseline = `resolve()`;
  `_SAFE_OVERRIDE_KEYS` + config-display defaults derived from the registry.
- `ops/config_parity.py` — diff YAML vs a `.env.compose` before cutover.
- `ops/gen_config_docs.py` → `docs/strategy_platform/CONFIG_REGISTRY_TABLE.md`.
- `docker-compose.gcp.yml` — mounts the YAML read-only into all strategy/sim services.
- Tests: `strategy_app/tests/test_config_loader.py` (10),
  `test_entry_pipeline_gates.py` MLEntryGate (6). All green.

---

## 1. Why we keep "playing with properties"

The pain is real and has three concrete root causes:

1. **Config has no single home.** Every tunable lives — with its own default —
   in **three** places that drift independently:
   - `.env.compose` (VM live values)
   - `ops_routes.py` `sim_env` dict (SIM rebuilds the env from scratch with its
     *own* hardcoded fallbacks)
   - the Python call site itself (`os.getenv("X", "default")`) — **404 of these
     across 69 files**
   When a new feature adds a var, you must update all three by hand or SIM/LIVE
   silently diverge. That is exactly the June‑12 "SIM shows 0 trades" bug:
   `ML_ENTRY_DIRECTION_MODE` defaulted to `composite` in `sim_env` but live ran
   `regime_dual`.

2. **Nothing is grouped.** The vars are flat strings. There is no "this whole
   subsystem is on/off", no nesting, no schema. You can't see at a glance what
   the entry stage vs the exit stage vs risk is doing.

3. **The LIVE↔SIM bridge is a manual mirror.** `main.py` hand-maintains
   `_ops_env_keys` (a list that must be kept in sync), writes `ops_env.json`,
   and `ops_routes.py` reads it back and re-defaults anything missing. Three
   hand-edited lists (`_ops_env_keys`, `sim_env`, `_SAFE_OVERRIDE_KEYS`) that
   all have to agree.

## 2. The target — your YAML, made real

```yaml
# ops/strategy_config.yml  ← THE single source of truth (live + sim read this)

entry:
  time_windows: "09:45-14:30"
  min_confidence: 0.65
  pipeline: v1                  # v1 | v2
  vol_gate:
    enabled: false
    atr_min_pct: 0.00088
  ml:
    min_prob: 0.65
    direction_mode: regime_dual # composite | regime_dual | consensus
    ml_weight: 0.40

regime:
  trend_score_min: 2.0
  aligned_bonus: 0.0
  vol_ratio_min: 1.30

direction:
  signal: weighted              # weighted | combo | agreement_lever
  min_margin_sideways: 2.0
  weights: { mom: 0.0, vwap: 0.5, maxpain: 0.8, oi: 0.8, ema: 0.5 }

exit:
  mode: adaptive                # scalper | adaptive
  max_loss_pct: 0.10            # universal floor, wraps every mode
  policy_stack_enabled: true
  scalper:
    hard_stop_pct: 0.07
    target_pct: 0.04
    thesis_fail_bars: 3
    thesis_fail_min_mfe: 0.002
  lottery:
    regimes: [TRENDING, BREAKOUT]
    hard_stop_pct: 0.20
    big_target_pct: 0.50
    runner_activation_mfe: 0.20
    runner_giveback_frac: 0.35
    thesis_fail_bars: 999       # 999 = disabled; timestop is the backstop
    thesis_fail_min_mfe: 0.03
    timestop_bars: 90
    momentum_flip: 1.0

strike:
  policy: smart_strike
  min_premium: 600
  max_premium: 1300
  max_otm_steps: 8

risk:
  capital: 500000
  per_trade_pct: 0.005
  max_consecutive_losses: 3
  max_session_trades: 20
  max_lots_per_trade: 5

execution:
  adapter: paper                # paper | dhan | kite | shadow
```

`.env.compose` shrinks to **secrets + infrastructure only** (broker tokens,
Redis host, Mongo URI, GCP project, telegram creds). No strategy numbers there
ever again.

## 3. The mechanism that makes it non-breaking

We do **not** touch the 404 `os.getenv()` call sites. Instead:

```
ops/strategy_config.yml
        │  load + flatten (via registry)
        ▼
  dict[str,str]  ──►  os.environ  (pushed once at process start)
        │                              │
   LIVE: main.py                  SIM: ops_routes._run_sim_thread
   pushes at startup              pushes, THEN layers operator overrides
```

Because the flattened config lands in `os.environ` **before** any engine code
runs, every existing `os.getenv("LOTTERY_HARD_STOP_PCT")` keeps working
unchanged. SIM calls the *same loader* on the *same file*, then applies overrides
on top. **Divergence becomes structurally impossible** — there is no second
default list to drift.

### The registry (kills the three hand-maintained lists)

`strategy_app/config/registry.py` holds ONE table — the only place a config key
is ever declared:

| field | purpose |
|---|---|
| `yaml_path` | `exit.lottery.hard_stop_pct` |
| `env_var` | `LOTTERY_HARD_STOP_PCT` (what the 404 call sites read) |
| `type` | `float / int / bool / str / csv` |
| `default` | fallback if absent from YAML |
| `group` | for docs + UI grouping |
| `sim_overridable` | replaces `_SAFE_OVERRIDE_KEYS` |
| `description` | one line — feeds the auto-generated doc table |

From this single table we *derive* (no hand-maintained lists):
- the YAML→env flattening
- `ops_env.json` keys (delete `_ops_env_keys` from main.py)
- `_SAFE_OVERRIDE_KEYS` (delete the hardcoded set in ops_routes.py)
- the whole of `05_CONFIG_REFERENCE.md` (a generator prints the tables)

## 4. Phased implementation (one at a time)

Each phase is shippable on its own and leaves the system working.

### Phase 1 — Loader + YAML, additive only *(no behavior change)*
- Create `ops/strategy_config.yml` populated from the *current* live
  `.env.compose` values (verbatim — this phase changes nothing).
- Add `strategy_app/config/registry.py` (the table) + `loader.py`
  (`load_config()` → flat dict, `apply_to_environ(precedence="env_wins")`).
- Call `apply_to_environ()` at the top of `main.py`. Use **`env_wins`**
  precedence first (existing `.env.compose` still authoritative) so Phase 1 is
  provably a no-op. Mount the YAML into both containers in compose.
- **Exit check:** `ops_env.json` identical before/after; a live restart trades
  the same.

### Phase 2 — SIM reads the loader *(kills the divergence)*
- Replace the entire `sim_env` dict in `ops_routes._run_sim_thread` with
  `load_config(YAML)` + operator overlay. Delete the hardcoded fallbacks.
- Derive `_SAFE_OVERRIDE_KEYS` from `registry` (`sim_overridable=true`).
- **Exit check:** SIM on 2026‑06‑12 fires the same trades LIVE took (this is
  the bug the user reported — it gets fixed *for good* here, not by patching
  one more default).

### Phase 3 — Flip authority to YAML, shrink `.env.compose`
- Switch `apply_to_environ` to **`yaml_wins`**; delete the migrated strategy
  vars from `.env.compose` (keep only secrets/infra).
- Delete `_ops_env_keys` from main.py; `ops_env.json` now derives from registry.
- **Exit check:** parity diff of resolved config before/after = empty.

### Phase 4 — Grouping + on/off switches + doc generator
- Add subsystem `enabled:` flags (`entry.vol_gate.enabled`,
  `exit.policy_stack_enabled`, etc.) wired through the registry.
- Add `ops/gen_config_docs.py` → regenerates `05_CONFIG_REFERENCE.md` from the
  registry so docs can never go stale again.
- Point `grid.yml` overrides at YAML paths (already compatible — overrides are
  still env-var keyed via the registry).

### Phase 5 *(optional, later)* — typed config object
- Introduce `config.exit.lottery.hard_stop_pct` accessors and migrate hot call
  sites off `os.getenv`. Purely cosmetic; only do this where it earns its keep.

## 5. Pipeline ordering (the "ML first, then direction" ask)

Already partially true: in `entry_pipeline_gates.py` the `DirectionGate`
short-circuits on the ML entry vote / `bypass_min_confidence` *before* resolving
direction. Phase 4 makes it explicit + reorderable by splitting it into:
- `MLEntryGate` — prob threshold only (fast veto, clear trace reason
  `ml_below_threshold`)
- `DirectionGate` — runs only if ML passed (`direction_vetoed`)

Gate order then becomes data in the registry/YAML, not code.

## 7. Live cutover on the VM (the one remaining step)

The code defaults to `yaml_wins`, but the YAML was built from the verified-live
2026-06-14 `.env.compose`, so the cutover should be a no-op. Verify, then deploy:

```bash
# 1. Copy the new code + YAML to the VM and into the running containers
gcloud compute scp --recurse strategy_app/config ops/strategy_config.yml \
    ops/config_parity.py option-trading-runtime-01:/opt/option_trading/...

# 2. PARITY CHECK against the real live config — must report no real differences
python ops/config_parity.py /opt/option_trading/.env.compose --strict
#    (exit 0 = safe. Any "REAL DIFFERENCES" = reconcile the YAML first.)

# 3. Recreate strategy_app + dashboard with the mounts (NOT docker restart)
sudo docker compose --env-file .env.compose up -d --no-deps strategy_app dashboard

# 4. Confirm: the startup log should show
#    "strategy_config applied: N/60 keys (precedence=yaml_wins, real_overrides=0)"
#    real_overrides=0 proves the cutover changed nothing.

# 5. Run a SIM for 2026-06-12 — it must now fire the 2 trades live took.

# INSTANT REVERT (no redeploy): set STRATEGY_CONFIG_PRECEDENCE=env_wins and restart.
```

Once verified, **shrink `.env.compose`** to secrets/infra only (broker tokens,
Redis host, Mongo URI, GCP project, telegram) — delete the migrated strategy
vars. After that the YAML is the sole place strategy numbers live.

## 6. What this buys us

- One file to read/change. Grouped, nested, with on/off switches.
- SIM and LIVE **cannot** diverge — same loader, same file.
- No more hunting "where does this default live" — the registry is the index.
- Docs auto-generated from the registry — never stale again.
- `999`, `0.00088`, `regime_dual` etc. are written once, visibly, in context.
