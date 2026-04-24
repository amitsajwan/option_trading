# Jira Issue: Historical Replay Chart - 500 Error & Incomplete Candle Data

## Summary
The historical replay chart is not displaying a complete set of per-minute OHLC candles. After deploying Parquet-based chart data loading to the `HistoricalReplayMonitorService`, the `/api/historical/replay/session` endpoint returns **500 Internal Server Error** with the message `historical replay service unavailable`. The root cause is a deployment gap: the dashboard container is running a stale GHCR image that does not contain the new code, and ad-hoc file patching inside the running container has led to a broken module initialization state where `_historical_replay_monitor_service` is `None` at application startup.

## Environment
- **Service:** `market_data_dashboard` (dashboard container)
- **VM:** `option-trading-runtime-01` (asia-south1-b)
- **Image:** `ghcr.io/amitsajwan/market_data_dashboard:latest`
- **Data source:** Parquet futures OHLC under `/app/.data/ml_pipeline/parquet_data/futures`
- **Branch:** `chore/ml-pipeline-ubuntu-gcp-runbook`

---

## Observations

### 1. Chart Data Completeness Issue (Original Bug)
- The historical replay chart only shows data points where triggered events exist, rather than a **continuous per-minute OHLC candle series** for the full market session (e.g., 09:15 to 15:30 IST).
- MongoDB snapshot collection `phase1_market_snapshots_historical` only stores snapshots when events occur, so it cannot provide the missing minutes.
- The canonical per-minute OHLC data exists in the Parquet futures dataset (`/app/.data/ml_pipeline/parquet_data/futures/year=YYYY/month=MM/*.parquet`).
- For `2024-07-02`, the Parquet dataset contains **376 rows** (one per minute), confirming the data is complete.

### 2. Code Changes Made (Working in Isolation)
The following files were modified in the local repo to fix the issue:

#### `market_data_dashboard/historical_replay_monitor_service.py`
- Added `_load_chart_from_parquet(date_ist, instrument)` static method to read OHLC from Parquet using `pyarrow` + `pandas`.
- Overrode `load_session_underlying_chart()` to prefer Parquet over MongoDB snapshots.
- Patched `get_historical_strategy_session()` to filter out `run_id` from `kwargs` before calling the parent `get_strategy_session()`, because the deployed parent method does not accept `run_id`.

#### `market_data_dashboard/historical_replay_routes.py`
- Added `run_id: Optional[str] = None` parameter to `get_historical_strategy_session()` route handler.
- Passed `run_id` through to the service call.
- Added graceful fallback for "not found" / "no completed" `ValueError` exceptions.

#### `docker-compose.gcp.yml`
- Added volume mount for the dashboard service:
  ```yaml
  volumes:
    - ./.data/ml_pipeline:/app/.data/ml_pipeline:ro
  ```
  This was missing in the GCP override, so the Parquet data was inaccessible from the dashboard container.

### 3. Deployment Gap - Stale GHCR Image
- The GitHub Actions workflow (`.github/workflows/build-images.yml`) only triggers builds on the **`main`** branch or tags (`v*`).
- **There is no `main` branch in this repository**; the active branch is `chore/ml-pipeline-ubuntu-gcp-runbook`.
- Therefore, pushing code to the current branch **does not trigger a CI build**, and the GHCR `latest` image remains stale.
- The VM's `docker-compose.gcp.yml` uses `pull_policy: always`, so every `docker compose up -d` pulls the old image and **discards any ad-hoc file patches**.

### 4. Ad-Hoc Patching Led to Broken State
To bypass the CI gap, the new Python files were copied directly into the running container and the container was restarted with `docker restart`.
- Direct service tests inside the container **pass successfully**:
  ```
  source: parquet_futures
  timestamps: 376
  closes: 376
  first ts: 2024-07-02T09:15:00+05:30
  last ts: 2024-07-02T15:30:00+05:30
  ```
- **However**, the HTTP endpoint `/api/historical/replay/session` returns **500** with `historical replay service unavailable`.
- In `app.py` (line ~1549), `_historical_replay_monitor_service` is instantiated at module load time:
  ```python
  _historical_replay_monitor_service = (
      HistoricalReplayMonitorService(_strategy_eval_service)
      if HistoricalReplayMonitorService is not None
      else None
  )
  ```
- There is **no `try/except`** around this initialization. If the constructor raises, the app would crash on startup.
- Since the app health checks pass (`/api/health` returns 200), the module must be importing without crashing, yet `_historical_replay_monitor_service` evaluates to `None`.
- The most likely explanation is that **`HistoricalReplayMonitorService` is not being imported successfully** in the `app.py` startup path (e.g., an import guard or conditional import is silently swallowing the import failure), or the patched module is incompatible with the rest of the older codebase in the container.

### 5. Container vs. Local Repo Mismatch
The running container's image is older than the local repo. Key mismatches observed:
- `live_strategy_monitor_service.py` in the container is missing the `run_id` parameter in `get_strategy_session()`.
- The route handler in the container originally did not accept `run_id`.
- These incompatibilities were patched ad-hoc, but the container's `app.py` startup logic was not designed for this mixed state.

---

## Steps to Reproduce
1. Open the Historical Replay page in the dashboard.
2. Select a historical date (e.g., `2024-07-02`) and a `run_id` (e.g., `6e0a7970-7a1c-407f-b359-394d8809e155`).
3. The frontend polls `/api/historical/replay/session`.
4. Observe **500 Internal Server Error** in the browser Network tab.
5. Inside the container, run:
   ```python
   from market_data_dashboard.historical_replay_monitor_service import HistoricalReplayMonitorService
   svc = HistoricalReplayMonitorService(evaluation_service=...)
   result = svc.get_historical_strategy_session(date='2024-07-02', run_id='...', ...)
   # This works and returns 376 candles.
   ```
6. The same call via HTTP endpoint fails with 500.

---

## Expected Behavior
- The `/api/historical/replay/session` endpoint should return a `session_chart` payload with **376 per-minute candles** for a full trading day.
- The `source` field should be `parquet_futures`.
- The chart should display a continuous line of opens, highs, lows, closes from 09:15 to 15:30 IST.

## Actual Behavior
- HTTP 500 with detail `historical replay service unavailable`.
- The `_historical_replay_monitor_service` singleton is `None` at runtime.

---

## Root Cause Analysis
1. **Missing CI trigger:** The repo has no `main` branch, so GitHub Actions never builds the `latest` image with the new code.
2. **Missing volume mount:** The GCP compose override did not mount the Parquet data directory, making the new code fall back to MongoDB (which lacks full-minute data).
3. **Ad-hoc patching failure:** Copying `.py` files into a running container and restarting it creates an inconsistent runtime state where `app.py` (from the old image) cannot properly instantiate the patched service class.

---

## Suggested Fix (for Expert Dev)

### Option A: Clean Local Build on VM (Fastest)
1. SSH to `option-trading-runtime-01`.
2. In `/opt/option_trading`, run:
   ```bash
   sudo docker build -f market_data_dashboard/Dockerfile -t ghcr.io/amitsajwan/market_data_dashboard:latest .
   ```
3. Temporarily change `pull_policy: always` to `pull_policy: never` (or remove it) for the `dashboard` service in `docker-compose.gcp.yml`.
4. Run:
   ```bash
   sudo docker compose -f docker-compose.gcp.yml --env-file .env.compose up -d dashboard
   ```
5. Verify:
   ```bash
   curl 'http://localhost:8008/api/historical/replay/session?date=2024-07-02&run_id=6e0a7970-7a1c-407f-b359-394d8809e155&limit_votes=10&limit_signals=10'
   ```

### Option B: Fix CI Pipeline (Proper Long-Term Fix)
1. Create a `main` branch (or update `.github/workflows/build-images.yml` to trigger on the current default branch).
2. Merge the changes from `chore/ml-pipeline-ubuntu-gcp-runbook` into `main`.
3. Let GitHub Actions build and push the new `latest` image to GHCR.
4. On the VM, run:
   ```bash
   cd /opt/option_trading
   sudo docker compose -f docker-compose.gcp.yml --env-file .env.compose pull dashboard
   sudo docker compose -f docker-compose.gcp.yml --env-file .env.compose up -d dashboard
   ```

### Additional Recommended Changes
- Add `try/except` logging around `_historical_replay_monitor_service` initialization in `app.py` so that future constructor failures are visible in logs instead of silently producing `None`.
- Consider mounting source code as a volume during development to avoid the image rebuild cycle.

---

## Files Modified in Local Repo
1. `market_data_dashboard/historical_replay_monitor_service.py` - Parquet chart loading + `run_id` filter
2. `market_data_dashboard/historical_replay_routes.py` - `run_id` parameter + graceful error handling
3. `docker-compose.gcp.yml` - Parquet volume mount for dashboard

## Attachments
- Container direct test output showing 376 candles from Parquet.
- Container logs showing 500 errors on `/api/historical/replay/session`.
- `app.py` initialization snippet showing `_historical_replay_monitor_service` construction without exception handling.
