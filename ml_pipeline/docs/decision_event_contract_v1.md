# Decision Event Contract V1 (T25)

This contract defines schema and semantic rules for decision/event JSONL emitted by inference adapters.

## Required Common Fields

Every row must include:

- `generated_at` (string, ISO timestamp)
- `timestamp` (string, ISO timestamp)
- `mode` (`dual|ce_only|pe_only`)
- `ce_prob` (number)
- `pe_prob` (number)
- `ce_threshold` (number)
- `pe_threshold` (number)
- `action` (`BUY_CE|BUY_PE|HOLD`)

Optional contextual fields:

- `trade_date` (string)
- `confidence` (number)

## Event Mode Fields (Exit-Aware Stream)

If `event_type` is present, it must be one of:

- `ENTRY`
- `MANAGE`
- `EXIT`
- `IDLE`

Additional requirements:

1. `event_reason` must be a non-empty string.
2. For `ENTRY|MANAGE|EXIT`, `position` must be an object with:
   - `side` (`CE|PE`)
   - `entry_timestamp` (string)
   - `entry_confidence` (number)
3. For `MANAGE|EXIT`, `held_minutes` must be a non-negative integer.

## Validation Output

Validator produces:

- total rows
- valid/invalid row counts
- invalid share
- error-type counts
- sample line-level errors (bounded)

Artifact:

- `ml_pipeline/artifacts/t25_decision_event_validation_report.json`
