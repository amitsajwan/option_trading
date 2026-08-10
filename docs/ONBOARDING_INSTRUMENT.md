# Onboarding a new instrument

Distilled from FINNIFTY's onboarding (2026-08-03/04) — the third instrument
after BankNifty and NIFTY. Read this before starting; run the preflight
checker throughout, not just at the end.

There is no one-click generator. This is a validation-first checklist: the
same checks the CI lint (`tests/test_instrument_hardcoding_lint.py`) runs are
exposed as a live preflight tool (`ops/onboard_instrument_check.py`) so you
always know exactly what's wired and what's TODO.

## 0. Before writing any code

```
python ops/onboard_instrument_check.py --instrument <NAME>
```

Run this first. It tells you what's missing. Re-run it after every step below.

## 1. Registry (the one thing the checker can't do for you)

Add an `InstrumentSpec` to `contracts_app/instruments.py`. Every field must
come from a **live** Dhan scrip-master lookup that day — never guess, never
copy a sibling instrument's numbers:

```python
from ingestion_app.dhan_client import ScripMaster
sm = ScripMaster.load(cache_dir="/tmp")
row = sm.find_nearest_futures("YOURNAME")
```

Confirms `index_security_id`, `lot_size` (from `SEM_LOT_UNITS`), and gives you
the real front-month contract symbol for step 4. `strike_step` and
`expiry_cadence` (weekly/monthly) come from the live option chain — check how
many strikes exist between listed contracts and whether both weekly and
monthly expiries are listed.

Mirror the same entry into `ml_pipeline_2/scripts/dhan_data_pipeline.py`'s
`INSTRUMENTS` dict (kept in sync by
`tests/test_instrument_hardcoding_lint.py`'s bidirectional parity check).

## 2. Code surfaces the linter checks for you

Run the preflight checker — it lists every missing surface with the exact
fix. As of FINNIFTY these are: `ingestion_app/dhan_ws_feed.py`'s
`_FUTURES_LABEL_ENV`, `ingestion_app/collectors/dhan_depth_collector.py`'s two
maps, and the compose/deploy.sh/config-contract completeness in Part 3 below.
**Do not hand-edit these from memory — trust the checker, it reads the actual
files.**

## 3. Compose — copy the pattern, not the values

Copy the closest sibling instrument's `_nifty`-suffixed (or `_banknifty`)
blocks across `docker-compose.yml`, `docker-compose.gcp.yml`,
`docker-compose.seller.yml` — same image, no new Dockerfile, only env/topic/
collection names differ. **Never reuse a shared, unnamespaced env var** for
anything that gates real money (`EXECUTION_ADAPTER`, `SELLER_LIVE_ENABLED`) —
give the new instrument its own `{INSTRUMENT}_EXECUTION_ADAPTER` etc., or
flipping it to paper for testing will silently flip an already-live sibling
too. This bit twice during FINNIFTY's onboarding.

Pick config values by matching the right *axis*, not the nearest instrument
overall — strike-distance params (condor offset/width) scale with strike
step; hold-days/entry-window params scale with expiry cadence; margin params
scale with notional (spot × lot). A monthly instrument with a 50-pt strike
step is NIFTY-like on one axis and BankNifty-like on another; don't copy one
sibling wholesale.

## 4. The deploy-overlay trap

Every VM command MUST use the full compose invocation:
```
docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml -f docker-compose.seller.yml
```
(`ops/deploy.sh`'s `$COMPOSE` var — copy it exactly). A bare `docker compose
up -d <service>` uses only the base file, which has stale/legacy defaults
(no Dhan creds, wrong instrument symbol) — this silently broke the *live*
BankNifty container for several minutes during FINNIFTY's rollout.

## 5. Data pipeline — verify cadence, don't assume it

If backfilling historical data, confirm the expiry-cadence logic used by the
backfill script is **registry-derived**, not a binary "if BANKNIFTY else
X" — that exact bug gave FINNIFTY's first backfill NIFTY's weekly expiry on
every day (found and repaired 2026-08-04, after 359 days were already
trained on). Spot-check `days_to_expiry` in the stored data directly; don't
trust that the code "probably" handles a third instrument correctly.

## 6. Monitoring (automatic once wired, verify it actually ran)

Feature-health, liveness, and config-contract checks are now instrument-
generic (derive from `known_instruments()` / a `docker ps` loop) — a new
instrument is covered automatically the moment its containers are running.
The feature-health and liveness systemd units still need re-installing on the
VM once (`sudo bash ops/gcp/install_dhan_token_units.sh`,
`sudo bash ops/gcp/install_liveness_units.sh`) to pick up the generic loop
script if they were installed before this generic rewrite landed.

## 7. The ML entry threshold — never copy a sibling's number

If the instrument has an ML entry model, its probability output range is its
own — a freshly trained model can have a hard ceiling well below a sibling's
threshold (FINNIFTY's v1 topped out at 0.54; BankNifty's live threshold is
0.60, which FINNIFTY's model could never reach). Compute it empirically:
score the bundle against a recent window of real feature data, pick a
threshold from the actual distribution (a sharp quantization cliff, if one
exists, is a natural and defensible cutoff), and verify the implied fire rate
looks like a real entry gate, not "never" or "constantly."

Also check whether the model's own training-time ship gates
(`ship_gates_all_pass` in its `.report.json`) passed — **nothing in the live
path enforces this at runtime** (`strategy_app/signals/entry_quality.py`'s
grading is entirely independent of the entry model's calibration quality), so
a model that failed its own gates needs extra manual caution: a conservative,
empirically-chosen threshold and a stricter `RISK_LIVE_MIN_GRADE` are the only
levers available to compensate.

## 8. Staged rollout before real money

1. Data plane only (`ingestion_app_X`, `snapshot_app_X`, `persistence_app_X`,
   `depth_collector_dhan_X`), brought up with `--no-deps` so no sibling
   container is touched. Verify: WS futures leg subscribed (check the owner
   container's logs, or call `ingestion_app.dhan_ws_feed.build_shared_legs`
   directly with a real scrip master — no live WS connection needed), Mongo
   collections populating.
2. Strategy plane in paper mode (`FINNIFTY_EXECUTION_ADAPTER=paper`,
   `SELLER_LIVE_ENABLED=0` equivalents). Verify decision traces are
   non-degenerate and observed probabilities roughly match the offline
   distribution used to pick the entry threshold.
3. `python3 ops/check_config_contract.py` passes with zero violations.
4. Dhan sandbox order-path check (`ops/gcp/dhan_sandbox_smoke.py` or
   equivalent) — confirms lot size and security-id resolution without
   touching real money. Run it against a sibling instrument too as a control
   if it fails (Dhan's sandbox itself can be down/unprovisioned — that's not
   your bug).
5. Flip live-money switches one at a time (buyer, then seller, or vice
   versa) — never both simultaneously — re-checking zero open positions on
   every OTHER instrument immediately before each flip.
6. Full `ops/deploy.sh` run (no args) so the new instrument is covered by
   every routine automated deploy going forward.

## 9. What's still manual by design

Compose-block generation from the registry was deliberately NOT built —
copy-and-adapt with the preflight checker catching gaps is judged safer than
a templating layer over a live-money deploy config, at least until a 4th
instrument's onboarding proves the pattern is stable enough to automate
further.

## 10. The exchange-segment trap (new with SENSEX, first cross-exchange instrument)

BankNifty/NIFTY/FINNIFTY are all NSE (`NSE_FNO`). `InstrumentSpec.fno_segment`
existed in the registry the whole time (default `"NSE_FNO"`) but was **dead
in the entire live path** — `execution_app/adapter/dhan.py`,
`ingestion_app/dhan_data_service.py` (4 sites), `ingestion_app/dhan_ws_feed.py`
(numeric WS segment code, not even a string), and
`ingestion_app/collectors/dhan_depth_collector.py` all hardcoded the literal
`"NSE_FNO"` instead of reading the registry field. Found and fixed as a
prerequisite phase (own PR, tested as a no-op against the existing 3
instruments) before SENSEX's registry entry was even added — see
`contracts_app/instruments.py`'s `FNO_SEGMENT_WS_CODE`/`FNO_SEGMENT_EXCHANGE`
tables. If instrument #5 lands on a third exchange, a new CI test
(`test_ws_segment_codes_cover_every_registered_fno_segment`) fails loudly if
its segment's numeric WS code isn't registered — don't skip adding it there.

### SENSEX-specific live facts (confirmed via scrip master + historical API calls, 2026-08-07)

- Index: `security_id="51"`, `SEM_CUSTOM_SYMBOL="Sensex"`, segment `IDX_I`
  (same index segment as NSE — no separate BSE index segment exists).
- **Trap: "SENSEX" and "SENSEX50" are two different, unrelated index
  families that share a naive string prefix.** `SEM_TRADING_SYMBOL` values
  `"SENSEX-Aug2026-FUT"` (lot 20, the real BSE Sensex) and
  `"SENSEX50-Aug2026-FUT"` (lot 75, a different index) both match
  `.startswith("SENSEX")`. Every symbol-family filter for SENSEX must split
  on `-` and compare the exact first token, never a prefix match — this is
  exactly the kind of cross-symbol collision the FNO-segment work's new
  `SEM_EXM_EXCH_ID` guard (onboarding doc step 2 / lint test) was added to
  catch, except this collision happens *within* BSE, not across exchanges.
- `fno_segment="BSE_FNO"`, lot_size=20 (from `SEM_LOT_UNITS` on `OPTIDX`
  rows), `strike_step=100` (confirmed via consecutive listed strikes).
- **Expiry weekday: Thursday, universally** — every listed expiry (7
  near-term weeklies through 2026-09-24, then monthly/quarterly further out
  through 2031) lands on a Thursday with zero exceptions. Simpler than
  NIFTY's Aug-2025 Thu→Tue SEBI cutover — `sensex_expiry()` needs no
  date-branching, unlike `nifty_weekly_expiry()`.
  `expiry_cadence="weekly"` (near-term weeklies exist, same shape as NIFTY's
  registry entry).
- Historical data coverage confirmed working identically to NSE via the
  standard SDK calls: index daily (`historical_daily_data`, `IDX_I`), futures
  daily and intraday minute (`BSE_FNO`) all returned real candle data. The
  options-specific historical endpoint (`expired_options_data` /
  `/charts/rollingoption`, used by `get_historical_day`'s per-day options
  legs) returned an identical accept/empty pattern for SENSEX and a NIFTY
  control across every parameter combination tried — no BSE-specific
  rejection or gap found. Verdict: **no evidence of a BSE historical-data
  gap; proceed with an immediate backfill-and-train timeline, same shape as
  FINNIFTY's**, not a weeks-long live-collection-only fallback. The exact
  working parameter recipe for options legs should be validated by exercising
  the codebase's own `get_historical_day()` path directly (once its
  `fno_segment` hardcode is fixed) rather than re-derived from a probe
  script.

### Cold-start training: the regime-shift trap (2026-08-09/10)

Backfilled 146 trading days (2026-01-01..2026-08-06, 54,822 snapshots,
verified clean — every listed expiry hit `dte=0` on its Thursday, zero
DTE anomalies across the whole window). First training attempt (train on the
full window, holdout on the most recent month) produced a **degenerate**
model: every holdout probability fell between 0 and 0.3, no separation at
any threshold. Root cause, confirmed by a monthly VIX/move-size breakdown:
SENSEX had a real volatility spike in March-April 2026 (VIX ~20-22) that
decayed back to baseline by July-August (VIX ~12, matching January). Train
was dominated by the spike; holdout landed in the calmest month in the
dataset — same failure shape already seen once with BankNifty ("quantified
regime shift", see project memory). Isotonic calibration fit on a
regime-mismatched validation set compressed every prediction toward the
lower, spike-era base rate.

Fix attempt #2 (shrink train/valid/holdout to a short, regime-consistent
window) made holdout AUC *worse* (0.546, barely above chance) — the shorter
window didn't have enough independent trading days (~7 each for valid/
holdout) to avoid HPO overfitting on a small, autocorrelated sample.

Fix attempt #3 (the one that worked): keep the **full 6 months for training**
(more data for the underlying fit) but confine **both** valid and holdout to
the same recent, regime-consistent window (2026-07-01 onward), so
calibration adapts to current conditions while the tree model still gets
enough data to generalize. Holdout AUC=0.6845 (still fails the formal ≥0.70
gate) but separation (0.35-0.67) and calibration (ECE=0.021) both pass, with
a real natural quantization cliff: the 0.6-0.7 probability bin was pure noise
(n=6, 0% hit rate) while 0.7-0.8 was strong (n=8, 75% hit rate vs an 8.4%
base rate). `ENTRY_ML_MIN_PROB=0.70` was picked from that cliff, not the
wider 0.6+ blend — same "pick the sharp bucket" call FINNIFTY's v1 launch
made. Small sample (n=8) backing the cliff — `RISK_LIVE_MIN_GRADE=GOOD` is
the compensating control, same posture as FINNIFTY's own cold start.

**Lesson for the next cold-start instrument:** when picking train/valid/
holdout windows for a brand-new instrument, check the *positive-rate trend
across the window first* (a simple monthly groupby), not just total row
count. A window that blends two different volatility regimes will train a
model whose calibration is right for neither.
