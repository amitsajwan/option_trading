# Scenario Testing Tools (ml_pipeline_2)

New tools for rapidly generating, comparing, launching, and analyzing staged manifest variations.

## Quick Start

### 1. Generate a manifest variation and diff it against baseline

```bash
cd ml_pipeline_2
python -m staged.scenario_cli --bypass-stage2 --diff --validate --run-name my_test
```

### 2. Write a manifest to disk

```bash
python -m staged.scenario_cli --bypass-stage2 --write-config /tmp/my_test.json --validate
```

### 3. Generate a full scenario matrix

```bash
python -m staged.scenario_cli --batch
# Generates 2 (bypass values) x 1 x 1 x 1 = 2 scenarios by default
```

### 4. Programmatic config building

```python
from ml_pipeline_2.staged.scenario_runner import build_manifest, write_manifest

m = build_manifest(
    bypass_stage2=True,
    run_name="expiry_bypass_test",
    stage1_threshold_grid=(0.45, 0.5, 0.55),
    stage3_margin_grid=(0.02, 0.05),
)
write_manifest(m, Path("/tmp/my_manifest.json"))
```

### 5. Compare two manifests

```python
from ml_pipeline_2.staged.config_diff import diff_manifests, print_diff
from ml_pipeline_2.staged.scenario_runner import build_manifest

baseline = build_manifest(bypass_stage2=False)
bypass = build_manifest(bypass_stage2=True)
print_diff(diff_manifests(baseline, bypass))
```

### 6. Batch launch on remote VM

```python
from ml_pipeline_2.staged.batch_launcher import BatchLauncher
from ml_pipeline_2.staged.scenario_runner import scenario_matrix

launcher = BatchLauncher()
scenarios = scenario_matrix(
    bypass_stage2_values=(False, True),
    stage1_threshold_values=((0.45, 0.5, 0.55), (0.5, 0.55, 0.6)),
)
launcher.queue_batch(scenarios)
launcher.launch_all(max_concurrent=2)

# Poll status
for r in launcher.poll_status():
    print(r["run_name"], r["status"])
```

### 7. Extract metrics from a completed run summary

```python
from ml_pipeline_2.staged.results_analyzer import extract_summary_metrics

m = extract_summary_metrics("/path/to/summary.json")
print(m.to_dict())
```

## Files Added

| File | Purpose |
|------|---------|
| `staged/scenario_runner.py` | Programmatic manifest builder and scenario matrix generator |
| `staged/config_diff.py` | Diff two manifests and print changes |
| `staged/scenario_cli.py` | CLI for `--diff`, `--validate`, `--batch`, `--launch-vm` |
| `staged/batch_launcher.py` | Upload manifests and launch runs on VM via tmux |
| `staged/results_analyzer.py` | Extract key metrics from `summary.json` and compare runs |
| `tests/test_bypass_stage2.py` | Unit tests for bypass_stage2 scoring and policy evaluation |

## Config Pathing Fix

`_validate_stage1_reuse` in `contracts/manifests.py` now searches
`artifacts/training_launches/*/*/run/runs/{source_run_id}` as a fallback when an
absolute `source_run_dir` does not exist. This makes grid configs portable across
machines.

## bypass_stage2 Pipeline Changes

When `training.bypass_stage2: true` is set in a manifest:

- **Stage 2 model training is skipped entirely**
- Dummy neutral probabilities are injected:
  - `direction_up_prob = 0.5`
  - `direction_trade_prob = 1.0`
  - `ce_prob = 0.5`, `pe_prob = 0.5`
- **Both CE and PE trades are evaluated independently** for every Stage 1 entry signal (`dual_side_mode`)
- Stage 3 recipe selection still runs on top of combined dual-side trades
- Stage 2 CV gate auto-passes

## Running the Full Pipeline on VM

### Prerequisites
- tmux installed on VM (`apt-get install tmux` if needed)
- SSH key configured locally
- VM IP: `34.47.131.234`, user: `savitasajwan03`

### Launch via wrapper script

Copy the wrapper to VM:
```bash
scp -i ~/.ssh/google_compute_engine run_bypass_tmux.sh savitasajwan03@34.47.131.234:/tmp/
ssh -i ~/.ssh/google_compute_engine savitasajwan03@34.47.131.234 "bash /tmp/run_bypass_tmux.sh"
```

The wrapper creates a tmux session named `bypass_stage2` that persists after SSH disconnects.

### Monitor progress

```bash
# Attach interactively
ssh -i ~/.ssh/google_compute_engine savitasajwan03@34.47.131.234 "tmux attach -t bypass_stage2"

# Or check pane content
ssh -i ~/.ssh/google_compute_engine savitasajwan03@34.47.131.234 "tmux capture-pane -pt bypass_stage2 -S -20"
```

### Check artifact status

```bash
ssh -i ~/.ssh/google_compute_engine savitasajwan03@34.47.131.234 "cat /home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research/expiry_bypass_stage2_test_v1_*/run_status.json"
```

## Known Issue: oracle_build Performance

On the VM, `oracle_build` (preparing labeled datasets for each recipe) can take
20-60 minutes for the full `snapshots_ml_flat_v2` dataset (449,704 rows x 120
columns x 7 recipes). The process appears idle because progress events are only
written at step boundaries. Use `top` or `ps` on the VM to confirm CPU/memory
usage if unsure.

## Next Steps

1. Once the bypass_stage2 run completes, use `results_analyzer` to extract metrics
2. Compare against the baseline `expiry_dir_grid_20260419_105402/01_expiry_s2_midday` run
3. If dual-side execution passes the combined holdout gate, promote
   `bypass_stage2` as default in the next training campaign
4. If it still fails, investigate Stage 1 entry signal quality or Stage 3 recipe
   model predictive power
