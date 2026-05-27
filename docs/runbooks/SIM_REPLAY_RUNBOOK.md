# SIM Replay Runbook

Use this runbook to operate SIM runs end-to-end from the dashboard API and monitor them in the UI.

## Prerequisites

- Dashboard API is healthy at `http://127.0.0.1:8008`
- Redis + Mongo are reachable by dashboard and strategy services
- `strategy_app_sim` is available in `docker compose --profile sim config --services`
- SIM orchestrator endpoints are mounted (`/api/sim/runs`)

Quick health check:

```bash
curl -fsS http://127.0.0.1:8008/api/health
```

## 1) Trigger a SIM Run via Curl

```bash
curl -fsS -X POST http://127.0.0.1:8008/api/sim/runs \
  -H "content-type: application/json" \
  -d '{
    "source_date":"2024-08-01",
    "source_coll":"phase1_market_snapshots",
    "label":"ops_manual",
    "speed":30,
    "env_overrides":{"STRATEGY_PROFILE_ID":"trader_master_v1"}
  }'
```

Expected response includes:

- `run_id`
- `manifest_path`
- `stream_name`
- `dashboard_url`

## 2) Inspect a Running SIM

Poll status:

```bash
curl -fsS http://127.0.0.1:8008/api/sim/runs/<RUN_ID>
```

List recent runs:

```bash
curl -fsS "http://127.0.0.1:8008/api/sim/runs?limit=20"
```

Check stream growth:

```bash
redis-cli XLEN stream:snapshots:sim:<RUN_ID>
```

Check per-run consumer container:

```bash
docker ps --format '{{.ID}} {{.Names}}' | grep strategy_app_sim || true
```

Check container logs:

```bash
docker logs <CONTAINER_ID> --tail 100
```

## 3) Compare Two Runs in UI (EVAL + LIVE tabs)

- Open `/app?mode=eval` and use the run filter with kind badges (`[SIM]` / `[OOS]`)
- Select two SIM run IDs and compare key metrics:
  - trade count
  - net return
  - win rate
  - drawdown profile
- For live-panel parity checks, open `/app?mode=live` and switch the watcher dropdown from `LIVE` to `SIM · <label> · <run_id_short>`

## 4) Debug Stuck / Cancelled Runs

Symptoms:

- Run status never leaves `running`
- No new stream entries
- Consumer exits early

Checks:

1. `GET /api/sim/runs/<RUN_ID>` for `status`, `terminal_status`, and metadata counts
2. `redis-cli XRANGE stream:snapshots:sim:<RUN_ID> - + COUNT 5` for sentinel presence
3. Container logs for stream or mongo errors
4. Verify `manifest.json`, `result.json`, or `cancellation.json` under run directory

Cancel a run:

```bash
curl -fsS -X DELETE http://127.0.0.1:8008/api/sim/runs/<RUN_ID>
```

Expected result: `status=cancelled` within a few seconds.

## 5) Manual Cleanup (TTL Override)

Mongo TTL should clean SIM collections automatically (30d), but manual cleanup is available.

Delete one run's docs:

```bash
python - <<'PY'
from pymongo import MongoClient
from contracts_app import resolve_namespace

run_id = "replace-me"
db = MongoClient("mongodb://localhost:27017")["trading_ai"]
ns = resolve_namespace("sim", run_id=run_id)
for base in ("snapshots","votes","signals","positions","decision_traces"):
    coll = ns.collection_for(base)
    n = db[coll].delete_many({"run_id": run_id}).deleted_count
    print(coll, n)
PY
```

Delete run directory:

```bash
rm -rf ".run/strategy_app_sim/<RUN_ID>"
```

## Scheduled GC (SIM-10)

Timer assets:

- `ops/cron/sim_gc.sh`
- `ops/cron/sim-gc.service`
- `ops/cron/sim-gc.timer`

Expected behavior:

- daily cleanup of run directories older than 30 days
- TTL audit log for stale docs in `*_sim` collections
- safe no-op when run directory root does not exist

Install on VM (as root):

```bash
sudo cp /opt/option_trading/ops/cron/sim-gc.service /etc/systemd/system/
sudo cp /opt/option_trading/ops/cron/sim-gc.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sim-gc.timer
sudo systemctl list-timers sim-gc.timer --no-pager
```

## Automated Smoke

Use the SIM smoke script:

```bash
bash ops/sim/smoke_test.sh
```

Script behavior:

- finds recent source date with enough snapshots
- creates SIM run through API
- polls until terminal state
- verifies manifest + sealed filesystem + run-tagged writes in `*_sim` collections
- performs best-effort delete cleanup
