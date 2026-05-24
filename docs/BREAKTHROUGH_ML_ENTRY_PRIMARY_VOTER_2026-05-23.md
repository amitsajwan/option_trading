# Breakthrough: ML_ENTRY as primary voter (2026-05-23)

## Summary

One integration fix in the deterministic engine unlocked trades the entry model was already scoring well. The edge is **vote-pool wiring**, not a new ML artifact.

| Metric | Before (`no_selection` starvation) | After (primary voter) |
|--------|--------------------------------------|------------------------|
| Trades | 25 | **61** |
| Profit factor | 1.25 | **1.98** |
| Win rate | 53% | 52.5% |
| Net cap PnL (₹100k base) | +0.20% | **+2.33%** |
| CE / PE | 14 / 3 (skewed) | **32 / 29** (both legs +PF) |
| `no_selection` blocker | ~1,408 | **1** |
| Vote → trade conversion | 0.7% | **3.8%** |

**Leg PF (post-fix, Aug–Oct 2024):** CE PF 1.93 (32), PE PF 2.10 (29).

## Root cause

Rule strategies that did not fire were treated as implicit vetoes. When `ML_ENTRY` had edge but composites/rules were silent, the council returned `no_selection` and no trade.

## Fix (frozen for OOS)

**Commit:** `a133936` — `ML_ENTRY` remains in the vote pool; **silence ≠ veto**; rules only block on active disagreement or risk gates.

Related risk-config commits (must be deployed together):

- `ffd5c83` — preserve profile `risk_config` across `set_run_context` (20% stop, not 40% default)
- `83bad06` / `3785248` — profile risk applied at startup

## Frozen runtime config (do not tune until OOS passes)

| Setting | Value |
|---------|--------|
| Profile | `trader_master_ml_entry_det_dir_v1` |
| Engine | `deterministic` |
| Entry model | `entry_only_model.joblib` (3 features: `day_of_week`, `ce_pe_oi_diff`, `ce_pe_volume_diff`) |
| `ENTRY_ML_MIN_PROB` | **0.65** |
| Stop / target / trail | 20% / 70% / 35% activation (from profile) |
| Direction | Deterministic rules (frozen; no direction ML) |
| Session caps | Default (`RISK_MAX_SESSION_TRADES=6`, consecutive losses=3) |

Patch on VM: `ops/gcp/patch_trader_master_ml_entry_det_dir_env.sh` with `ENTRY_ML_MIN_PROB=0.65`.

## In-sample reference window

**Aug–Oct 2024** (`2024-08-01` → `2024-10-31`) — breakthrough metrics above. Prior CLEAN baseline (`46efdc16`): 56 trades, PF 1.21, net +4.1%, net w/o top-5 **-5.2%** (outlier-sensitive).

## What is working vs still suboptimal

**Working**

- Balanced CE/PE with both legs profitable at 61-trade sample
- Trailing stops firing more (4 → 12 exits) — partial profit lock
- First `TARGET_HIT` in this profile line
- `no_selection` effectively gone

**Suboptimal (defer until OOS passes)**

- `risk_pause` — ~379 blocked snapshots (consecutive-loss / daily DD)
- `session_trade_cap` — ~310 blocks (6 trades/session default)
- ~70% `TIME_STOP` exits — MFE giveback / stagnation tuning
- Council exit layer not yet in play

## Orthogonal work (do not conflate)

Overnight entry HPO v2 (May 2026) reached CV AUC ~0.65 but **publish HOLD** (0 holdout trades at 0.5 threshold). That path uses `entry_bn_5m_100pts_v1` + oracle economics — **not** the runtime vote-pool fix. Do not swap entry weights before OOS validates the integration win.

## Next step

See [runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md](runbooks/OOS_VALIDATION_ML_ENTRY_PRIMARY_VOTER.md).
