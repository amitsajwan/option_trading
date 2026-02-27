# Exit Policy Contract V1 (T14)

This document defines the strict config contract for exit-management policies used by Phase 2 tasks.

## Policy Object

JSON object fields:

1. `version` (`string`, required): must be `"v1"`.
2. `time_stop_minutes` (`int`, required): `[1, 60]`.
3. `stop_loss_pct` (`float`, required): `(0, 1]`.
4. `take_profit_pct` (`float`, required): `(0, 2]` and must be `> stop_loss_pct`.
5. `enable_trailing_stop` (`bool`, required).
6. `trailing_stop_pct` (`float|null`):
   - required and in `(0, 1]` if trailing stop enabled
   - must be `null` if trailing stop disabled
7. `move_to_break_even_at_profit_pct` (`float|null`): if provided, in `(0, 2]`.
8. `allow_hold_extension` (`bool`, required).
9. `max_hold_extension_minutes` (`int`, required):
   - `[1, 30]` if hold extension enabled
   - `0` if hold extension disabled
10. `extension_min_model_prob` (`float|null`):
    - required and in `[0, 1]` if hold extension enabled
    - `null` if hold extension disabled
11. `forced_eod_exit_time` (`string`, required):
    - format `HH:MM` (24-hour)
    - must be within NSE session `[09:15, 15:30]`

Unknown fields are rejected.

## Default Policy

```json
{
  "version": "v1",
  "time_stop_minutes": 3,
  "stop_loss_pct": 0.12,
  "take_profit_pct": 0.24,
  "enable_trailing_stop": false,
  "trailing_stop_pct": null,
  "move_to_break_even_at_profit_pct": null,
  "allow_hold_extension": false,
  "max_hold_extension_minutes": 0,
  "extension_min_model_prob": null,
  "forced_eod_exit_time": "15:24"
}
```

## Validation Command

```powershell
$env:PYTHONPATH = "$PWD\ml_pipeline\src"
python -m ml_pipeline.exit_policy --report-out ml_pipeline\artifacts\t14_exit_policy_validation_report.json --normalized-out ml_pipeline\artifacts\t14_exit_policy_config.json
```

## Artifacts

- `ml_pipeline/artifacts/t14_exit_policy_validation_report.json`
- `ml_pipeline/artifacts/t14_exit_policy_config.json`
