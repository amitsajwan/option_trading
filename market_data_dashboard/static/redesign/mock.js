// Plausible mock data for the four-page trading dashboard prototype.
// Deterministic seeded random so layouts don't jitter between refreshes.

(function (global) {
  let seed = 42;
  function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
  function rndBetween(a, b) { return a + rnd() * (b - a); }

  // Instruments watchlist
  const instruments = [
    { sym: 'NIFTY 50',      ex: 'NSE IDX', px: 22341.15, chg: +184.35, chgPct: +0.83 },
    { sym: 'BANKNIFTY',     ex: 'NSE IDX', px: 47992.80, chg: -121.40, chgPct: -0.25 },
    { sym: 'RELIANCE',      ex: 'NSE EQ',  px: 2871.45,  chg: +42.10,  chgPct: +1.49 },
    { sym: 'HDFCBANK',      ex: 'NSE EQ',  px: 1528.10,  chg: -8.25,   chgPct: -0.54 },
    { sym: 'TCS',           ex: 'NSE EQ',  px: 3941.85,  chg: +17.30,  chgPct: +0.44 },
    { sym: 'INFY',          ex: 'NSE EQ',  px: 1504.60,  chg: +6.15,   chgPct: +0.41 },
    { sym: 'NIFTY 26APR 22300 CE', ex: 'NFO OPT', px: 118.25, chg: +21.40, chgPct: +22.09 },
    { sym: 'NIFTY 26APR 22300 PE', ex: 'NFO OPT', px: 64.50,  chg: -18.35, chgPct: -22.14 },
  ];

  // Give each instrument a sparkline series
  instruments.forEach((ins, idx) => {
    const dir = ins.chg >= 0 ? 1 : -1;
    const arr = [];
    let v = ins.px - ins.chg;
    for (let i = 0; i < 48; i++) {
      v += (rnd() - 0.5) * (ins.px * 0.003) + (dir * ins.px * 0.00015);
      arr.push(v);
    }
    arr[arr.length - 1] = ins.px;
    ins.spark = arr;
  });

  // Build 1-min candles for the active symbol
  function buildCandles(n, startPx) {
    const candles = [];
    let p = startPx;
    // start of day 09:15 IST
    const startMin = 9 * 60 + 15;
    for (let i = 0; i < n; i++) {
      const o = p;
      const range = p * 0.0025;
      const c = o + (rnd() - 0.48) * range;
      const h = Math.max(o, c) + rnd() * range * 0.4;
      const l = Math.min(o, c) - rnd() * range * 0.4;
      const v = Math.floor(rndBetween(800, 4200));
      const m = startMin + i;
      const hh = Math.floor(m / 60), mm = m % 60;
      const label = `${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`;
      candles.push({ t: i, o, h, l, c, v, label });
      p = c;
    }
    return candles;
  }
  const candles = buildCandles(225, 22157.0);

  // Trade markers scattered across session
  const trades = [
    { t: 18,  price: candles[18].l - 4, side: 'buy',  shape: 'triangle', label: 'B1' },
    { t: 34,  price: candles[34].h + 4, side: 'sell', shape: 'triangle', label: 'S1' },
    { t: 62,  price: candles[62].l - 4, side: 'buy',  shape: 'triangle', label: 'B2' },
    { t: 97,  price: candles[97].h + 4, side: 'sell', shape: 'triangle', label: 'S2' },
    { t: 134, price: candles[134].l - 4, side: 'buy', shape: 'triangle', label: 'B3' },
    { t: 168, price: candles[168].h + 4, side: 'sell', shape: 'triangle', label: 'S3' },
    { t: 195, price: candles[195].l - 4, side: 'buy', shape: 'triangle', label: 'B4' },
  ];
  // Signal markers (ML votes that didn't fire)
  const signals = [
    { t: 22, price: candles[22].c, side: 'info', shape: 'triangle', label: '' },
    { t: 55, price: candles[55].c, side: 'info', shape: 'triangle', label: '' },
    { t: 110, price: candles[110].c, side: 'info', shape: 'triangle', label: '' },
  ];

  // Trade log (closed trades)
  const closedTrades = [
    { t: '09:33', strat: 'mom_breakout_v3',  dir: 'LONG',  qty: 75,  entry: 22169.30, exit: 22211.80, pnl: +0.19, pnlPct: +0.19, hold: '16m' },
    { t: '09:49', strat: 'mean_rev_band_v2', dir: 'SHORT', qty: 75,  entry: 22221.45, exit: 22198.60, pnl: +0.10, pnlPct: +0.10, hold: '08m' },
    { t: '10:17', strat: 'mom_breakout_v3',  dir: 'LONG',  qty: 150, entry: 22235.00, exit: 22221.10, pnl: -0.06, pnlPct: -0.06, hold: '12m' },
    { t: '11:02', strat: 'gap_fade_v1',      dir: 'SHORT', qty: 75,  entry: 22288.70, exit: 22247.35, pnl: +0.19, pnlPct: +0.19, hold: '21m' },
    { t: '11:48', strat: 'ml_ensemble_v5',   dir: 'LONG',  qty: 150, entry: 22253.15, exit: 22314.20, pnl: +0.27, pnlPct: +0.27, hold: '14m' },
    { t: '12:24', strat: 'mean_rev_band_v2', dir: 'SHORT', qty: 75,  entry: 22331.05, exit: 22339.40, pnl: -0.04, pnlPct: -0.04, hold: '07m' },
    { t: '13:36', strat: 'mom_breakout_v3',  dir: 'LONG',  qty: 75,  entry: 22295.55, exit: 22341.15, pnl: +0.20, pnlPct: +0.20, hold: '31m' },
  ];

  // Strategy votes (latest few)
  const votes = [
    { t: '14:02:11', strat: 'mom_breakout_v3',  dir: 'LONG',  conf: 0.78, fired: true },
    { t: '14:02:11', strat: 'mean_rev_band_v2', dir: 'FLAT',  conf: 0.41, fired: false },
    { t: '14:02:11', strat: 'gap_fade_v1',      dir: 'FLAT',  conf: 0.22, fired: false },
    { t: '14:02:11', strat: 'ml_ensemble_v5',   dir: 'LONG',  conf: 0.71, fired: true },
    { t: '14:01:11', strat: 'mom_breakout_v3',  dir: 'LONG',  conf: 0.64, fired: false },
    { t: '14:01:11', strat: 'mean_rev_band_v2', dir: 'SHORT', conf: 0.58, fired: false },
    { t: '14:01:11', strat: 'ml_ensemble_v5',   dir: 'LONG',  conf: 0.52, fired: false },
  ];

  // Alerts
  const alerts = [
    { t: '14:01:48', level: 'warn', msg: 'Depth staleness on NIFTY 26APR — last tick 4.2s ago (threshold 3.0s)' },
    { t: '13:52:17', level: 'info', msg: 'Strategy <strong>ml_ensemble_v5</strong> promoted LONG signal at conf 0.71' },
    { t: '13:36:02', level: 'info', msg: 'Trade closed <strong>mom_breakout_v3</strong> +0.20% on NIFTY, hold 31m' },
    { t: '13:14:55', level: 'crit', msg: 'Redis consumer lag on <strong>ohlc.1min</strong> = 6.8s — auto-backoff engaged' },
    { t: '12:58:03', level: 'warn', msg: 'Indicator feed age 2.1s approaching SLO (2.5s)' },
    { t: '12:02:11', level: 'info', msg: 'Session clock advanced to 12:00 — lunch-window filter engaged' },
    { t: '09:15:00', level: 'info', msg: 'Session open — <strong>4 strategies armed</strong>, warm-up complete' },
  ];

  // Strategy returns (bar chart)
  const strategyReturns = [
    { label: 'mom_breakout_v3',  value: +0.89 },
    { label: 'ml_ensemble_v5',   value: +0.64 },
    { label: 'gap_fade_v1',      value: +0.22 },
    { label: 'vwap_reversion',   value: +0.08 },
    { label: 'mean_rev_band_v2', value: -0.04 },
    { label: 'breakout_retest',  value: -0.18 },
  ];

  // Equity curve
  function buildEquity(n, finalPct, dd) {
    const arr = [0];
    for (let i = 1; i < n; i++) {
      const target = (finalPct / n) * i;
      const jitter = (rnd() - 0.5) * 0.35;
      arr.push(+(arr[i-1] * 0.3 + target * 0.7 + jitter).toFixed(3));
    }
    // inject a drawdown
    if (dd) {
      const start = Math.floor(n * 0.55);
      for (let i = start; i < start + 12 && i < n; i++) arr[i] -= (i - start) * 0.05;
    }
    return arr;
  }
  const equityCurrent = buildEquity(90, 1.62, true);
  const equityBaseline = buildEquity(90, 0.98, false);

  // Per-trade PnL series for histogram
  const tradePnls = Array.from({length: 48}, () => +(rndBetween(-0.35, 0.55)).toFixed(2));

  // depth ladder
  const depthBid = [
    { px: 22340.95, sz: 1250 },
    { px: 22340.80, sz: 2475 },
    { px: 22340.65, sz: 1800 },
    { px: 22340.50, sz: 3120 },
    { px: 22340.35, sz: 2200 },
  ];
  const depthAsk = [
    { px: 22341.15, sz: 1400 },
    { px: 22341.30, sz: 2100 },
    { px: 22341.45, sz: 1650 },
    { px: 22341.60, sz: 2800 },
    { px: 22341.75, sz: 3200 },
  ];

  // Historical replay: list of session days
  const sessionDays = [
    { d: '2026-04-16', trades: 8,  net: +0.84, status: 'completed' },
    { d: '2026-04-15', trades: 11, net: +0.41, status: 'completed' },
    { d: '2026-04-14', trades: 6,  net: -0.18, status: 'completed' },
    { d: '2026-04-11', trades: 9,  net: +1.12, status: 'completed' },
    { d: '2026-04-10', trades: 7,  net: +0.29, status: 'completed' },
    { d: '2026-04-09', trades: 12, net: -0.44, status: 'completed' },
    { d: '2026-04-08', trades: 8,  net: +0.66, status: 'completed' },
    { d: '2026-04-07', trades: 5,  net: +0.12, status: 'completed' },
  ];

  global.MOCK = {
    instruments, candles, trades, signals, closedTrades, votes, alerts,
    strategyReturns, equityCurrent, equityBaseline, tradePnls,
    depthBid, depthAsk, sessionDays,
  };
})(window);
