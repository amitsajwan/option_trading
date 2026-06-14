# Config & Code Changes — Safe Operations Guide

> **Why this doc exists:** every major system failure traced back to one of four
> unsafe patterns. This doc defines the rules so we don't repeat them.

---

## Rule 1: Never Apply a Fix via `docker cp` (Hot-Patch)

### What happened
`docker cp` copies a file into a running container. The fix lives only in that
container layer. Any `docker-compose up --force-recreate` or container restart
rebuilds from the image — and the patch is gone silently.

**2026-06-13 incident:** fix `c635738` (shadow score refresh) was applied via
`docker cp strategy_app/engines/... container:/app/...`. Reverted by the next
`--force-recreate` for an ATR override. The fix was invisible in backtests for a
full session.

### The Rule
**All code fixes must be:**
1. Committed to the feature branch
2. Rebuilt into the Docker image (`docker-compose build <service>` or full rebuild)
3. Deployed via `docker-compose up -d`

If you need a temporary override for one experiment:
- Use an **environment variable** (set via `-e` flag or in `.env.compose`) — these survive recreate
- Never use `docker cp` for logic changes

---

## Rule 2: One `.env.compose` — Delete the Other

There are two `.env.compose` files:
- `/opt/option_trading/.env.compose` — 185-line brain config (**THIS IS CORRECT**)
- `.deploy/runtime-config/.env.compose` — 85-line old C1 config (**DELETE THIS**)

The C1 config has:
- `EXECUTION_ADAPTER=paper` (correct) but `stop_loss_pct=0.25` (no policy stack)
- Missing brain/LLM/vol-gate config
- Deploys the wrong entry engine

When rebuilding the runtime VM, **ONLY** restore from the 185-line version.
The GCS runtime bundle (`gs://amit-trading-option-trading-runtime-config/runtime`)
should contain the 185-line version — verify before deploy.

---

## Rule 3: Pin Model-Serving Versions in Dockerfiles

**2026-06-13 incident:** model trained on xgboost 3.2.0, served on 2.1.4 →
all bars produced prob=0.826. Ran for weeks because there was no startup check.

**Fixed by:** `_check_xgboost_version()` in `bundle_inference.py` — now logs
ERROR if train/serve versions differ. But the real fix is pinning:

```dockerfile
# In dashboard/Dockerfile and strategy_app/Dockerfile
RUN pip install xgboost==3.2.0
```

Check the training environment's pinned version from the published bundle metadata
(`bundle["xgboost_version"]`). If absent, check the ML VM's pip freeze.

---

## Rule 4: Run Config Audit Before Every Deploy

`ops/config_audit.py` reads the running container env and flags:
- CONFLICTS (two keys that contradict each other)
- DEAD (keys that have no effect given the current mode)
- AMBIGUOUS (unclear which of two code paths is active)

```bash
# On the GCP runtime VM:
docker exec strategy_app printenv | python -m ops.config_audit -
```

Fix all CONFLICT-level issues before starting a real-money session.

---

## Rule 5: Stale Mongo Replay — Always Use `run_id` Filter

`strategy_positions_historical` uses a **deterministic `_id`** (derived from
`event_id`, not timestamp). Old replay runs are never cleared — they upsert in place.

**Result:** mixing runs from May 23-26 (1,982 VOL_GATE_ENTRY trades) with a fresh
June-13 run (3 trades) gave a contaminated aggregate. Every P&L number from May was
wrong.

**Safe aggregation:**
```python
# Always filter by run_id from the fresh session
fresh_run_id = "..."  # from the replay driver's output
db.strategy_positions_historical.aggregate([
    {"$match": {"run_id": fresh_run_id}},
    ...
])
```

Or use the **in-process harness** (`c:/tmp/backtest_2024.py`) which never writes to
mongo — trades come back in-memory per day. This is the canonical backtest method.

---

## Checklist Before Any Live Config Change

- [ ] Which `.env.compose` is the source? (should be `/opt/option_trading/.env.compose`, 185-line)
- [ ] Is `EXIT_STRATEGY_MODE=adaptive`? → Must set BOTH `EXIT_SCALPER_HARD_STOP_PCT` AND `LOTTERY_HARD_STOP_PCT`
- [ ] Did you change a Python file? → Commit + `docker-compose build` + `docker-compose up -d` (not `docker cp`)
- [ ] Is a model being served? → Check startup logs for `INTEGRITY WARNING: model trained on xgboost ...`
- [ ] Any NaN features logged at WARNING? → Investigate which features are null before going live
- [ ] Run `config_audit.py` → resolve all CONFLICTs
