# Research Recovery Runbook

This runbook is the top-level reconstruction guide for the MIDDAY staged-model recovery work.

## Goal

Recover a publishable staged model by:
- keeping Stage 1 fixed once it was proven acceptable
- solving Stage 2 probability quality first
- then moving downstream to Stage 3 policy and combined holdout economics

## Sequence

1. Stage 2 MIDDAY redesign
- Doc: `ml_pipeline_2/docs/stage2_midday_redesign.md`
- Purpose: freeze Stage 2 to `MIDDAY`, improve feature representation and label cleanliness

2. Stage 2 target redesign
- Doc: `ml_pipeline_2/docs/stage2_midday_target_redesign.md`
- Purpose: prune ambiguous directional rows before Stage 2 training

3. Stage 2 high-conviction attempts
- Doc: `ml_pipeline_2/docs/stage2_midday_high_conviction.md`
- Purpose: test whether a narrower binary direction problem could clear publish gates
- Outcome: useful negative evidence; not the final path

4. Stage 2 direction-or-no-trade redesign
- Doc: `ml_pipeline_2/docs/stage2_midday_direction_or_no_trade.md`
- Purpose: replace forced `CE vs PE` with hierarchical:
  - trade vs no-trade
  - then `CE vs PE`
- Outcome: Stage 2 CV cleared again

5. Stage 3 policy-path recovery
- Doc: `ml_pipeline_2/docs/stage3_midday_policy_paths.md`
- Purpose: freeze the proven Stage 2 setup and search downstream policy and recipe-selection paths
- Current active batch

6. Stage1+Stage2 counterfactual analysis
- Doc: `ml_pipeline_2/docs/stage12_counterfactual.md`
- Purpose: test whether top-confidence Stage1+Stage2 trades already contain usable economic edge before another Stage 3 redesign
- Use after a completed Stage 3 policy-path run when the winner still fails economics

## Current batch

Config files:
- `ml_pipeline_2/configs/research/staged_dual_recipe.stage3_policy_paths_v1.json`
- `ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json`

Key implementation notes:
- `selection.ranking_strategy = stage3_policy_paths_v1` scopes Stage3-first ranking to this grid only
- `reuse_stage2_from` is allowed only when the Stage 2 target definition remains compatible
- expanded recipe-catalog lanes reuse Stage 1 only, because Stage 2 targets depend on the recipe catalog
- fixed Stage 3 fallback is a real runtime policy mode:
  - `selection_mode = fixed_recipe`
  - `selected_recipe_id` must be valid

## Run on GCP

Pull:

```bash
cd ~/option_trading
git fetch origin
git checkout chore/ml-pipeline-ubuntu-gcp-runbook
git pull --ff-only origin chore/ml-pipeline-ubuntu-gcp-runbook
git log --oneline -n 5
ls -la ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json
```

Run in `tmux`:

```bash
tmux kill-session -t stage3_midday_policy_paths_v1 2>/dev/null || true
cd ~/option_trading
mkdir -p ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1
tmux new -s stage3_midday_policy_paths_v1
python -m ml_pipeline_2.run_staged_grid \
  --config ml_pipeline_2/configs/research/staged_grid.stage3_midday_policy_paths_v1.json \
  --run-output-root ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run \
  --run-reuse-mode fail_if_exists \
  --model-group banknifty_futures/h15_tp_auto \
  --profile-id openfe_v9_dual \
  2>&1 | tee ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/training-grid.log
```

Detach:

```bash
Ctrl+b d
```

Health checks:

```bash
pgrep -af "ml_pipeline_2.run_staged_grid|run_staged_release"
tail -50 ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/training-grid.log
```

Finish check:

```bash
cd ~/option_trading && . .venv/bin/activate && python - <<'PY'
import json
from pathlib import Path

p = Path("ml_pipeline_2/artifacts/training_launches/stage3_midday_policy_paths_v1/run/grid_summary.json")
data = json.loads(p.read_text())
print("status:", data.get("status"))
print("winner:", json.dumps(data.get("winner"), indent=2))
print("dominant_failure_reason:", data.get("dominant_failure_reason"))
print("stage2_hpo_escalation:", json.dumps(data.get("stage2_hpo_escalation"), indent=2))
PY
```

## Interpretation guide

The current batch is acceptable only if the winner:
- preserves a strong Stage 2 result
- removes `stage3.non_inferior_to_fixed_recipe_baseline_failed`
- produces `net_return_sum > 0`
- produces `trades >= 50`
- keeps `side_share_in_band == True`

If all policy paths still fail:
- stop policy-only iteration
- run the Stage1+Stage2 counterfactual analysis
- if oracle or fixed-recipe subsets improve materially, move to Stage 3 label/view redesign
- if they do not, question the upstream economic edge before opening another Stage 3 batch

## Minimal restart checklist

If this work has to be reconstructed from scratch, recover in this order:
- read this runbook
- read the four stage docs in sequence
- confirm the branch and commit
- validate the current Stage3 grid manifest resolves
- run the Stage3 policy-path grid
- evaluate the winner and only then choose the next redesign branch
