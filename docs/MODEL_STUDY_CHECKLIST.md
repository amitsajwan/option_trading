# Model study checklist — training, backtesting, before any deploy claim

Every item here traces to a real mistake made (and caught) during actual
model studies on this repo, not a hypothetical. Run through this before
trusting or presenting any training/backtest result — for a brand-new
model, a retrain, a threshold change, or a label-definition change alike.

## Before training

- [ ] **Confirm the training/valid/holdout windows don't leak into each
  other.** train_end < valid_start < valid_end < holdout_start <
  holdout_end, with no overlap. Check `training_metadata` on the bundle
  after training, don't just trust the CLI args were applied correctly.
- [ ] **If comparing multiple label definitions or target sizes, hold the
  horizon and windows fixed across all of them** — vary exactly one thing
  at a time. Different horizons often live in different scripts with
  hardcoded values (`train_entry_rarelabel_v1.py`=5min,
  `train_entry_v3label_mongo.py`=15min in this repo) — check which one a
  given instrument's live model actually uses before picking a script.
- [ ] **Reuse an existing training view if the date range already
  matches** — check `.manifest.json` next to any `.csv.gz` before
  rebuilding. Rebuilding a 100k+ row view from Mongo takes real time for
  no benefit if one already covers the range.
- [ ] **Don't run two XGBoost training processes "in parallel" on the same
  box expecting a speedup.** Each process likely uses all available cores
  internally (OpenMP) — 2 "parallel" jobs on a 4-core box is actual 2x
  oversubscription, not 2x throughput. Run sequentially, or explicitly cap
  each process's threads (`OMP_NUM_THREADS`) to a fair share first.
- [ ] **Don't launch an unrelated CPU/IO job (e.g. a parquet export) at the
  same time as a training run "just to save time.**" It contends and
  slows both down. Let one finish, or explicitly verify there's real
  spare capacity first (`uptime`, `free -h`) — and even then, expect some
  slowdown, not none.

## Reading a trained model's results — don't trust one number

- [ ] **A model's own "best value" during HPO (the validation-set score
  Optuna optimized) is NOT the same as its true holdout AUC.** A model can
  post the highest validation score in a whole study and still have the
  *worst* holdout AUC — classic overfitting to the validation set. Always
  read the actual `holdout: AUC=...` line, never the HPO trial's "Best
  value" as a stand-in for it.
- [ ] **Check ship gates, not just AUC.** `auc>=0.70`, `separation>=0.15`,
  `ece<=0.05`, `prob_spread>=0.20` — a model can have a good AUC and still
  fail every other gate.
- [ ] **Check the holdout probability RANGE, not just whether ship gates
  technically pass.** Pull `min`/`max` of the holdout probabilities (or
  the reliability table's populated bins). A model whose probabilities
  never exceed ~0.1–0.3 anywhere is structurally incapable of firing at
  any normal live threshold, regardless of what its AUC says — this
  exact shape caused FINNIFTY's total live dormancy
  (`project_finnifty_dormancy_root_cause_2026-09-05.md`).
- [ ] **Read the full separation_table, not just one threshold.** Look for
  `"degenerate": true` entries and for precision/separation that's flat or
  near-zero everywhere — a model can be "not degenerate" (has a full 0-1
  range) while still being nearly useless in practice (e.g. NIFTY's 0.20%
  target: full range, but only 14-20% precision at every threshold).

## Picking a threshold to backtest

- [ ] **Always pick a model's threshold from ITS OWN separation_table.**
  Never reuse another model's (or the same model's older version's)
  threshold on a retrain — this exact mistake made FINNIFTY's v2 model
  structurally unable to ever fire (threshold above its live-observed
  ceiling), and the bug went undetected for the model's entire live
  history until traced back explicitly.
- [ ] If a model shows multiple distinct usable regimes in its separation
  table (e.g. a moderate-frequency/moderate-precision zone AND a sharp
  high-precision/low-frequency zone), **test more than one threshold** —
  don't assume the "sharper" one is automatically better; check both in
  the actual backtest (a NIFTY case this session showed the "sharper"
  threshold did NOT actually win on real P&L).

## Before running the capital-weighted backtest

- [ ] **Verify the backtest date range is actually covered by parquet
  data** before assuming — check with `available_days()` first. The
  default `PARQUET_BASE` in this repo typically only covers a recent
  ~2-month window (varies); a 6-month backtest almost always needs a
  fresh instrument-specific export via
  `ops/export_mongo_snapshots_to_parquet.py`.
- [ ] **⚠️ THE BIG ONE — check whether the backtest window overlaps the
  model's own TRAINING window.** If a model trained through
  `train_end=2026-06-15` and the backtest runs `2026-02-01..2026-07-31`,
  most of that window (Feb–mid-June) is DATA THE MODEL ALREADY SAW during
  training — the backtest is measuring partial memorization, not genuine
  forward-looking skill, for that portion. **A capital-weighted backtest
  is only a clean test of real generalization on the days strictly AFTER
  `holdout_end`** (or better, on a window that postdates the whole
  train/valid/holdout split entirely, so it isn't even influenced by
  which days happened to fall in the calibration/valid set). This was
  caught mid-session (2026-09-09) when a model already confirmed
  structurally broken on its true holdout data posted the *best* backtest
  numbers of an entire study over a window that included its training
  period — see `project_backtest_insample_contamination_finding_2026-09-09.md`
  for the full incident and which prior results it puts a caveat on.
  **Prefer backtesting on a window that starts strictly after
  `holdout_end`, ideally with a gap.**
- [ ] **Confirm the instrument's real lot size from the actual live
  `.env.compose`** (SSH in and grep it), not the bare fallback default in
  `docker-compose.gcp.yml` — the two can differ (e.g. BankNifty's
  compose-file default is 15, but the real configured value is 30).
- [ ] Confirm the live model's actual currently-deployed threshold the
  same way (grep the running container's env, or `.env.compose`) before
  using it as the "Config A" baseline — don't assume it matches an old
  memory or a training report's default.

## Reading backtest results

- [ ] **Report trade counts prominently, next to every percentage.** A
  return figure from under ~15-20 live trades is not distinguishable from
  noise — say so explicitly rather than letting a clean-looking percentage
  imply more confidence than the sample supports.
- [ ] **If a result looks surprisingly good (or bad) relative to what the
  offline ship-gate/separation evidence predicted, don't report it at
  face value — investigate first.** This is the single most valuable habit
  from this whole session: a "confirmed broken" model posting the best
  backtest numbers in a study is not a pleasant surprise, it's a bug
  (in this case, in-sample contamination) to find before it misleads a
  real decision.
- [ ] **Account for how many configs/thresholds/instruments have been
  tried before presenting a "winner."** Across a long study touching many
  instruments and configs, the single best-looking number carries less
  evidence than it appears to — a real, if informal, multiple-comparisons
  caveat. Report the full table, not just the top result.
- [ ] Note explicitly when a result is a single backtest with no live
  validation yet — recommend a monitored live TRIAL (not a full
  unmonitored real-money switch) as the next step for anything genuinely
  promising, matching how prior deploys in this repo were staged.

## Before ever deploying anything from a study

- [ ] Ship gates passing (or a clearly reasoned, explicit exception).
- [ ] A capital-weighted backtest on a window clean of the training-window
  contamination above.
- [ ] The user's explicit go-ahead — every time, regardless of how good a
  result looks. A good backtest is necessary, never sufficient, for a
  real-money change.
