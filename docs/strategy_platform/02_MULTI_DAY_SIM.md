# 02 — Multi-Day Sim (the immediate next project)

*The only honest test of a lottery strategy: replay many historical days through the
engine with a given config, aggregate the distribution, and compare configs.*

---

## 1. Why this is the priority

One day cannot validate lottery. Its premise is **rare big-move days pay for many
small-loss days** — a fat-tail bet. 2026-06-01 had no tail (max +12%), so lottery ≈
scalper. To know which strategy is actually better you must see the *distribution of
daily outcomes* over a representative sample (target: 20–60 trading days).

The existing OPS tool sims **one day** from `events.jsonl`. This project generalises it
to a **date range** from parquet, with portfolio-level aggregation.

---

## 2. Goal & success criteria

**Goal:** given a config (scalper or lottery, with any overrides) and a date range,
produce per-day results and an aggregate report that answers:

- Cumulative P&L over the period
- Daily win rate (fraction of green days)
- **Max drawdown** (the number that kills lottery if the tail never comes)
- Distribution of daily returns (histogram; is there a fat right tail?)
- Expectancy, profit factor, avg trades/day
- **Head-to-head: scalper vs lottery on the same days**

**Success = a defensible answer to "does lottery beat scalper over N days on P&L AND
drawdown?"** with the data to back it.

---

## 3. Logic / algorithm

```
INPUT: config (env overrides), date_range [d0..dN]
FOR each trading day d in range:
    snapshots = load_parquet_snapshots(d)          # ordered intraday
    engine = build_engine(config)                  # fresh per day (clean session)
    engine.on_session_start(d)
    set_run_context(run_id=f"sim-{d}", merged_profile_risk_config)   # see fidelity rules
    trades_d = []
    FOR snap in snapshots:
        signal = engine.evaluate(snap)
        track entries/exits -> trades_d   (read pnl/mfe/mae from tracker closed_positions)
    engine.on_session_end(d)
    day_result[d] = {trades, pnl, wins, mfe, exits, ...}

AGGREGATE across days:
    cumulative_pnl, daily_returns[], win_days/total_days,
    max_drawdown(cumulative_pnl_curve),
    expectancy = mean(trade_pnl), profit_factor = sum(wins)/abs(sum(losses)),
    fat_tail_days = count(daily_return > +X%),
    histogram(daily_returns)

OUTPUT: per-day table + aggregate report (+ optional A/B vs a second config)
```

### Reuse, don't reinvent
- The per-day inner loop is essentially `_run_engine()` in
  `market_data_dashboard/routes/ops_routes.py`. Extract it into a shared module
  (`strategy_app/sim/replay_engine.py`) that both OPS-today and multi-day call.
- Snapshot loading: a parquet reader for `.data/ml_pipeline/parquet_data/snapshots/`.
  An existing historical replay path exists (`snapshot_app/historical/replay_runner.py`,
  `docker-compose` `historical` profile) — evaluate reusing it vs a direct parquet read.

### Critical correctness requirements (from sim fidelity, doc 01 §5)
- Fresh engine + `on_session_start` per day (no state bleed between days).
- Merge profile risk_config (don't overwrite).
- `STRATEGY_RUN_DIR=/tmp/...`, `STRATEGY_REDIS_PUBLISH_ENABLED=0` — never touch live.
- ML library versions pinned (already enforced in dashboard image).
- Validate: a no-override multi-day run over a date that overlaps a known live day must
  reproduce that day's live numbers.

---

## 4. Metrics — definitions (be precise; traders will challenge these)

| Metric | Definition |
|---|---|
| Daily return | sum of trade `pnl_pct` for the day (premium-relative, before costs unless cost model on) |
| Cumulative P&L | running sum of daily returns (or compounded — state which) |
| Max drawdown | max peak-to-trough decline of the cumulative curve |
| Win rate (trade) | winning trades / total trades |
| Win rate (day) | green days / total days |
| Expectancy | mean trade `pnl_pct` |
| Profit factor | Σ winning pnl / |Σ losing pnl| |
| Fat-tail day | day with return > threshold (e.g. +5%) — the lottery payoff days |
| Capture ratio | mean(pnl/mfe) over trades with mfe>0 (see Findings §3 — can be negative) |

Decide and **document** simple-sum vs compounded returns, and whether transaction costs
(`TRANSACTION_COST_PER_LOT`) are on. Apples-to-apples between scalper and lottery is the
whole point.

---

## 5. Architecture / where it lives

Two delivery options — pick in design review:

**Option A — extend the OPS tool (recommended first):**
- New endpoint `POST /api/ops/sim/range` `{date_from, date_to, overrides}` → job_id.
- Background job loops days, reuses the shared replay module, streams progress.
- OPS UI gains a "date range" mode + an aggregate panel (equity curve, drawdown,
  histogram, A/B columns).
- Pro: reuses everything; operator-driven; no new container.
- Con: runs in the dashboard process (fine for 20–60 days; not for 1000s).

**Option B — batch CLI / job:**
- `python -m strategy_app.sim.multi_day --from --to --config ... --out report.md`
- Writes `docs/reports/multiday_<range>_<config>.md` + a CSV of per-day rows.
- Pro: scriptable, cron-able, large ranges, CI-friendly.
- Con: no live UI.

Both should call the **same** extracted replay module so results are identical.

---

## 6. Stories, tasks, Definition of Done

### Epic MD — Multi-Day Sim

**MD-S1: Extract shared replay module.** *(Points: 3)*
Move the per-day engine loop out of `ops_routes._run_engine` into
`strategy_app/sim/replay_engine.py` (`replay_day(snapshots, trade_date, config) -> [trades]`).
OPS-today calls it. No behaviour change.
- DoD: OPS "sim today" produces identical numbers to before the refactor (regression
  test on 2026-06-01: 12 trades / +6.58% scalper). Unit test for `replay_day`.

**MD-S2: Parquet multi-day snapshot loader.** *(Points: 3)*
`load_day(date) -> ordered snapshots` from `.data/ml_pipeline/parquet_data/snapshots/`.
Handle missing days, holidays, partial days.
- DoD: loads a known historical day and the snapshot count/shape matches the live JSONL
  shape the engine expects (`SnapshotAccessor` works on it). Test on ≥3 historical days.

**MD-S3: Multi-day runner + aggregation.** *(Points: 5)*
Loop days, fresh engine per day, collect per-day results, compute the §4 metrics +
max drawdown + daily-return histogram.
- DoD: produces per-day table + aggregate for a ≥20-day range. Max-drawdown and
  profit-factor unit-tested against a hand-computed fixture.

**MD-S4: A/B harness (scalper vs lottery).** *(Points: 3)*
Run two configs over the identical day set; emit side-by-side aggregates + per-day diff.
- DoD: one command outputs both columns + the winner on P&L and on drawdown. Same day
  set guaranteed (no silent date mismatch).

**MD-S5: Report output.** *(Points: 2)*
Markdown report to `docs/reports/multiday_<range>.md`: summary table, equity curve
(ASCII or PNG), drawdown, histogram, per-day rows, config used, git commit.
- DoD: report regenerates deterministically for a fixed range+config+commit.

**MD-S6: OPS UI range mode (if Option A).** *(Points: 5)*
Date-range picker; aggregate panel (equity curve, drawdown, histogram); A/B columns.
- DoD: operator runs a 20-day scalper-vs-lottery comparison from the drawer and reads
  the verdict without touching a terminal.

**MD-S7: Fidelity validation.** *(Points: 2)*
Confirm a no-override multi-day run reproduces live numbers on overlapping days.
- DoD: documented proof (a day where multi-day sim == live to the decimal).

### Definition of Done (applies to every story)
1. Code merged to `mordenization`, reviewed by ≥1 engineer.
2. Unit tests for new logic; existing tests green.
3. Sim-fidelity rules (doc 01 §5) upheld — no writes to live state, config from
   `ops_env.json`/explicit, ML versions pinned.
4. A validation run is captured in a report under `docs/reports/`.
5. One-paragraph doc update here or in the report.

---

## 7. First experiment to run once built

`scalper` vs `lottery (protected runner: act=10%, give=35%, timestop=90b, momentum=off)`
over the last 20–40 trading days. Read: cumulative P&L, max drawdown, fat-tail day count.
**Decision rule:** lottery is adopted only if it beats scalper on cumulative P&L *and*
does not have materially worse max drawdown — or if its drawdown is acceptable for the
upside. Document the call with the numbers.
