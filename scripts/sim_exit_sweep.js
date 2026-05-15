// Counterfactual exit sweep on C1's 107 baseline trades, with train/valid/holdout window split.
// Compare 30-bar (Phase 1.2 baseline) vs 15-bar (proposed shorter hold) vs 9-bar (C1 original)
// to see which exit profile actually works on the holdout window.

const C1_RUNS = [
  "0f0dfb36-8bed-447d-b36e-b208db1f5288",
  "a8c930e0-f5cb-4589-b9aa-eef7a5f36eef",
];

const STOP_PCT   = 0.002;   // 20 bps adverse underlying
const TARGET_PCT = 0.005;   // 50 bps favorable underlying
const COST_FRAC  = 0.02;    // 200 bps round-trip on premium

const C1_TRAIN_END   = "2024-04-30";
const C1_VALID_END   = "2024-07-31";
const C1_HOLDOUT_END = "2024-10-31";

function windowOf(date) {
  if (!date) return "unknown";
  if (date <= C1_TRAIN_END)   return "train";
  if (date <= C1_VALID_END)   return "valid";
  if (date <= C1_HOLDOUT_END) return "holdout";
  return "post-holdout";
}

function optionLtp(payload, direction, strike) {
  const strikes = payload?.snapshot?.strikes || [];
  for (const row of strikes) {
    if (Number(row.strike) === Number(strike)) {
      const ltp = direction === "CE" ? row.ce_ltp : row.pe_ltp;
      if (typeof ltp === "number" && ltp > 0) return ltp;
    }
  }
  return null;
}

function futureSnaps(date, entryTs, n) {
  return db.phase1_market_snapshots_historical
    .find({trade_date_ist: date, timestamp: {$gt: entryTs}}, {payload: 1, timestamp: 1, _id: 0})
    .sort({timestamp: 1}).limit(n).toArray();
}

function simulate(entries, maxHold) {
  const out = [];
  for (const e of entries) {
    const date = e.trade_date_ist;
    const entryTs = e.timestamp;
    const direction = e.direction;
    const strike = e.strike;
    const entryPrem = e.entry_premium;
    const entryFut = e.entry_futures_price;
    if (entryPrem == null || entryFut == null || direction == null) continue;

    const fwd = futureSnaps(date, entryTs, maxHold);
    if (fwd.length === 0) continue;

    let exitReason = null, exitPrem = null;
    for (let i = 0; i < fwd.length; i++) {
      const s = fwd[i];
      const fut = s.payload?.snapshot?.futures_bar?.fut_close;
      if (typeof fut !== "number" || fut <= 0) continue;
      const move = (fut - entryFut) / entryFut;
      const dirMove = direction === "PE" ? -move : move;
      if (dirMove >= TARGET_PCT) { exitReason = "TARGET_HIT"; exitPrem = optionLtp(s.payload, direction, strike); break; }
      if (dirMove <= -STOP_PCT) { exitReason = "STOP_LOSS";  exitPrem = optionLtp(s.payload, direction, strike); break; }
    }
    if (exitReason === null) {
      exitReason = "TIME_STOP";
      exitPrem = optionLtp(fwd[fwd.length - 1].payload, direction, strike);
    }
    if (exitPrem == null) continue;
    out.push({date, direction, pnl: (exitPrem - entryPrem) / entryPrem, exitReason});
  }
  return out;
}

function statsFor(trades) {
  if (trades.length === 0) return null;
  const n = trades.length;
  const wins = trades.filter(t => t.pnl > 0).length;
  const gross = trades.reduce((a, t) => a + t.pnl, 0);
  const wAbs = trades.filter(t => t.pnl > 0).reduce((a, t) => a + t.pnl, 0);
  const lAbs = trades.filter(t => t.pnl < 0).reduce((a, t) => a + (-t.pnl), 0);
  const pf = lAbs > 0 ? wAbs / lAbs : Infinity;
  return {
    n, wins,
    win_pct: wins / n * 100,
    avg_gross_pct: gross / n * 100,
    net_at_200bps_pct: (gross - n * COST_FRAC) * 100,
    pf_gross: pf,
  };
}

function bucketByWindow(trades) {
  const buckets = {train: [], valid: [], holdout: [], "post-holdout": []};
  for (const t of trades) {
    const w = windowOf(t.date);
    if (buckets[w]) buckets[w].push(t);
  }
  return buckets;
}

function reportFor(label, results) {
  print(`\n========== ${label} (max_hold=${results.maxHold} bars) ==========`);
  print(`Total simulated: ${results.trades.length}`);
  const overall = statsFor(results.trades);
  const buckets = bucketByWindow(results.trades);
  const lines = [];
  lines.push(["window", "n", "avg_gross%", "net@200bps%", "PF", "win%"]);
  for (const w of ["train", "valid", "holdout"]) {
    const s = statsFor(buckets[w]);
    if (s) lines.push([w, s.n, s.avg_gross_pct.toFixed(2), s.net_at_200bps_pct.toFixed(2), s.pf_gross.toFixed(2), s.win_pct.toFixed(1)]);
    else   lines.push([w, 0, "-", "-", "-", "-"]);
  }
  if (overall) lines.push(["OVERALL", overall.n, overall.avg_gross_pct.toFixed(2), overall.net_at_200bps_pct.toFixed(2), overall.pf_gross.toFixed(2), overall.win_pct.toFixed(1)]);

  for (const row of lines) {
    print("  " + row.map((v, i) => String(v).padStart(i===0 ? 14 : 11)).join("  "));
  }
}

// Load C1 entries
const entries = db.strategy_positions_historical
  .find({event: "POSITION_OPEN", run_id: {$in: C1_RUNS}})
  .toArray();
print(`Loaded ${entries.length} C1 baseline entries`);

// Run sims at multiple hold values
const variants = [9, 15, 20, 30];
const results = {};
for (const h of variants) {
  results[h] = {maxHold: h, trades: simulate(entries, h)};
  print(`\nDone: ${h}-bar hold → ${results[h].trades.length} simulated trades`);
}

// Per-variant report
for (const h of variants) {
  reportFor(`${h}-BAR HOLD`, results[h]);
}

// Holdout-only side-by-side
print(`\n\n========== HOLDOUT-ONLY COMPARISON (the test that matters) ==========`);
print(`  ${"hold_bars".padStart(10)}  ${"n".padStart(4)}  ${"avg_gross%".padStart(11)}  ${"net@200bps%".padStart(12)}  ${"PF".padStart(6)}  ${"win%".padStart(6)}  exit_mix`);
for (const h of variants) {
  const buckets = bucketByWindow(results[h].trades);
  const s = statsFor(buckets["holdout"]);
  const exitCount = {};
  for (const t of buckets["holdout"]) exitCount[t.exitReason] = (exitCount[t.exitReason] || 0) + 1;
  const mix = Object.entries(exitCount).map(([k, v]) => `${k}=${v}`).join(",");
  if (s) {
    print(`  ${String(h).padStart(10)}  ${String(s.n).padStart(4)}  ${s.avg_gross_pct.toFixed(2).padStart(11)}  ${s.net_at_200bps_pct.toFixed(2).padStart(12)}  ${s.pf_gross.toFixed(2).padStart(6)}  ${s.win_pct.toFixed(1).padStart(6)}  ${mix}`);
  }
}

print(`\n========== TRAIN-WINDOW COMPARISON (sanity, where everything should look good) ==========`);
print(`  ${"hold_bars".padStart(10)}  ${"n".padStart(4)}  ${"avg_gross%".padStart(11)}  ${"net@200bps%".padStart(12)}  ${"PF".padStart(6)}  ${"win%".padStart(6)}`);
for (const h of variants) {
  const buckets = bucketByWindow(results[h].trades);
  const s = statsFor(buckets["train"]);
  if (s) {
    print(`  ${String(h).padStart(10)}  ${String(s.n).padStart(4)}  ${s.avg_gross_pct.toFixed(2).padStart(11)}  ${s.net_at_200bps_pct.toFixed(2).padStart(12)}  ${s.pf_gross.toFixed(2).padStart(6)}  ${s.win_pct.toFixed(1).padStart(6)}`);
  }
}
