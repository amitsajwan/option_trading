# strategy_app

Layer-4 strategy consumer runtime. See **[`docs/README.md`](docs/README.md)** for full documentation.

Quick start:

```bash
python -m strategy_app.main --engine deterministic
python -m strategy_app.main --engine ml_pure --ml-pure-model-package <path> --ml-pure-threshold-report <path>
```

Tests: `pytest strategy_app/tests/`
