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
