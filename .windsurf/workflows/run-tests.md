---
description: Run all test suites for the option trading project
---

# Run Tests Workflow

## Activate venv first (Windows)

```powershell
.venv\Scripts\activate
```

## Strategy engine tests

```bash
python -m pytest strategy_app/tests/ -q
```

Known: 14 failures due to `TradeSignal.lots` attribute removal (tests not yet updated).

## Dashboard tests

```bash
python -m pytest market_data_dashboard/ -q
```

## Integration/boundary tests

```bash
python -m pytest tests/ -q
```

Known: 1 failure in `test_live_runtime_boundaries.py` — asserts `deterministic` as historical default, but compose now uses `ml_pure`.

## ML pipeline tests

```bash
python -m pytest ml_pipeline_2/tests/ -q
```

## Frontend type check

```bash
cd strategy_eval_ui
npm install
npx tsc -b --noEmit
```

## All tests (combined)

```bash
python -m pytest tests/ strategy_app/tests/ market_data_dashboard/ -q --tb=short
```
