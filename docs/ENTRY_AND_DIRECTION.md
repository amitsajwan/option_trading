# Entry and direction — two-track training

## Runtime (sequential, exclusive layers)

```mermaid
flowchart TD
  A[11:30 snapshot] --> B[Entry: playbook rules ± S1 ML]
  B -->|block| Z[No trade]
  B -->|allow| C[Rules: CE / PE votes]
  C -->|one side| D[Trade]
  C -->|CE+PE conflict| E[Direction ML overlay]
  E --> D
```

1. **Entry** decides *whether* to trade (playbook primary; optional Stage-1 model later).
2. **Direction** decides *CE vs PE* only when rules disagree (`DIRECTION_ML_MODEL_PATH`).

Direction never overrides a blocked entry.

---

## Track A — Entry (do this first)

| Item | Path |
|------|------|
| Manifest | `ml_pipeline_2/configs/research/staged_dual_recipe.entry_s1_only_hpo_v1.json` |
| Launcher | `python -m ml_pipeline_2.scripts.run_entry_s1_only_hpo` |
| VM script | `ops/gcp/run_entry_s1_only_hpo_vm.sh` |
| Flags | `bypass_stage2`, `bypass_stage3`, `entry_only_publish` |
| Gates | `hard_gates.stage1` + `hard_gates.entry_only` (economic holdout) |

**Rules-only entry (no ML):** PBV1 rule matrices + deterministic eval replays — still the main production path until S1 HPO passes.

```bash
sudo docker compose ... down   # free RAM
sudo bash /opt/option_trading/ops/gcp/run_entry_s1_only_hpo_vm.sh
```

ETA ~1–2 h (oracle + S1 Optuna).

---

## Track B — Direction (after entry is acceptable)

| Item | Path |
|------|------|
| Manifest | `ml_pipeline_2/configs/research/staged_dual_recipe.direction_s2_only_hpo_v1.json` |
| Launcher | `python -m ml_pipeline_2.scripts.run_direction_s2_only_hpo` |
| VM script | `ops/gcp/run_direction_s2_only_hpo_vm.sh` |
| Export | `python -m ml_pipeline_2.scripts.export_direction_bundle_from_research --run-dir ...` |
| Runtime | `DIRECTION_ML_MODEL_PATH` → `strategy_app/ml/direction_ml_policy.py` |

```bash
sudo bash /opt/option_trading/ops/gcp/run_direction_s2_only_hpo_vm.sh
```

See also [DIRECTION_S2_ONLY.md](DIRECTION_S2_ONLY.md).

---

## Do not use for decoupled research

| Manifest | Problem |
|----------|---------|
| `direction_only_hpo_v1` | Still trains S1+S2+S3; combined gates → often 0 trades |
| `stage1_hpo.json` (legacy menu) | S1-focused HPO but **still runs S2+S3** |

---

## VM checklist

```bash
cd /opt/option_trading
sudo git pull --ff-only origin main
sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml down

# Track A (entry ML research)
sudo bash ops/gcp/run_entry_s1_only_hpo_vm.sh validate
sudo bash ops/gcp/run_entry_s1_only_hpo_vm.sh

# Track B (direction ML) — after A or in parallel if RAM allows (not both heavy jobs)
sudo bash ops/gcp/run_direction_s2_only_hpo_vm.sh validate
sudo bash ops/gcp/run_direction_s2_only_hpo_vm.sh
```

Unified host: [GCP_UNIFIED_VM.md](GCP_UNIFIED_VM.md).

---

## Experiment profile — ML entry + trader_master exits

| Item | Value |
|------|--------|
| Profile | `trader_master_ml_entry_v1` |
| Entry | **`ML_ENTRY` only** (+ `IV_FILTER` veto) — no ORB/PBV1/rule entry strategies |
| Exits / risk | Same as `trader_master_v1` (ORB, OI, composites, top-3, PBV1 exit helpers, trailing) |
| Export S1 | `python -m ml_pipeline_2.scripts.export_entry_bundle_from_research --run-dir ...` |
| Env | `ENTRY_ML_MODEL_PATH`, `ENTRY_ML_MIN_PROB` (default 0.55); optional `DIRECTION_ML_MODEL_PATH` for CE/PE |
| Patch VM | `ops/gcp/patch_trader_master_ml_entry_env.sh` |

Replay example:

```bash
# After export on VM:
sudo bash ops/gcp/patch_trader_master_ml_entry_env.sh
sudo docker compose ... build strategy_app_historical
sudo docker compose ... up -d --force-recreate strategy_app_historical
# historical eval with STRATEGY_PROFILE_ID=trader_master_ml_entry_v1
```
