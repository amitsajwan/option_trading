# Dynamic Exit Policy Spec V1 (T17)

T17 introduces a configurable policy-state module for dynamic exits, independent of model training.

## Objectives

1. Deterministic exit state transitions.
2. Configurable TP/SL/trailing/break-even behavior.
3. Hold-extension logic based on model confidence.

## Core API

Module: `ml_pipeline.dynamic_exit_policy`

- `DynamicExitPolicyConfig`
- `validate_policy_config(config)`
- `simulate_dynamic_exit(entry_price, bars, horizon_minutes, model_prob, config)`

## Config Fields

- `stop_loss_pct`
- `take_profit_pct`
- `enable_trailing_stop`
- `trailing_stop_pct`
- `move_to_break_even_at_profit_pct`
- `allow_hold_extension`
- `max_hold_extension_minutes`
- `extension_min_model_prob`
- `intrabar_tie_break` (`sl|tp`)

## Exit Reasons

Returned `exit_reason` values:

- `tp`
- `sl`
- `trail`
- `time`
- `invalid`

## Determinism Rule

If TP and SL are both touched in one bar:

- `intrabar_tie_break=sl` => stop-side outcome (`sl` or `trail`)
- `intrabar_tie_break=tp` => `tp`

## Hold Extension Rule

Extension is allowed only if all are true:

1. `allow_hold_extension=true`
2. `model_prob >= extension_min_model_prob`
3. `max_hold_extension_minutes > 0`

## CLI

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.dynamic_exit_policy --out ml_pipeline\artifacts\t17_dynamic_exit_policy_report.json
```

## Output Artifact

- `ml_pipeline/artifacts/t17_dynamic_exit_policy_report.json`
