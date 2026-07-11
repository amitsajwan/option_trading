# Monday-open checklist — 2026-07-13

Deploy batch (Sunday evening, BEFORE 09:15 IST Monday; one deploy, then verify):
- [ ] `sudo bash ops/deploy.sh` at latest commit (fill-truth 00b947d + IV persistence a78c25e + enrichment 8c5b285 already on VM repo)
- [ ] Config contract PASS in deploy output
- [ ] Seller containers see calendar: `docker exec option_trading-seller_app-1 grep refreshed_at /app/config/event_calendar.json`

At 08:20 IST:
- [ ] PREMARKET brief arrives on Telegram (dev-box task `PremarketBriefClaude`)

At 09:15-09:30 IST:
- [ ] Both sellers publish fresh status (dashboard :8008/seller LIVE tab, no OFF-HOURS pill)
- [ ] snapshot_app logs show `iv-state restored` (after Monday's first deploy-restart it will be a cold save; restore proves out on the NEXT restart)
- [ ] **EMA-serving check (the live half of the A4 finding)**: seller quiet-gate scores should sit in the training-like range. Grep seller logs for any `entry_skipped_movement_risk` / status score field; if scores hug ~0.04-0.06 all morning on a moving market, the live path may ALSO lack EMA — escalate before trusting the fire-line.
- [ ] `movement_signals` collection: expect docs only if score >= 0.30 (rare; absence on a calm day is fine)

At 09:50 IST:
- [ ] FIRST-30 check arrives on Telegram and its numbers match the dashboard

During the day:
- [ ] Tue 2026-07-14 = US CPI: sellers must SKIP entries (`entry_skipped_event` in logs) — first live event-veto firing
- [ ] NIFTY condor expires Tue 07-14: watch the first live seller EXIT lifecycle
- [ ] Any live buyer entry: verify fill-truth behavior — rejected orders must produce `position CANCELLED` within 5 bars, not hours of phantom management

After close:
- [ ] Live-vs-backfill cross-check: backfill Monday via hist pipeline, compare a few live snapshot features (esp. ema_9/ema_50_slope, iv_percentile, max_pain) against the backfilled versions of the same bars

Pending decision (user): VM instance schedule for cost trim (~₹18.5k -> ~₹8-10k/mo at
08:00-20:00 IST runtime). NOT enabled — overnight batch jobs (backfills, replays)
still run ad hoc; enabling a stop schedule without a job-guard would kill them.
