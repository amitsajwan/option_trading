// multi-instrument.jsx — Multi-Instrument Strategy Dashboard (Stories S1–S6)
// Consumes the backend-team instrument-aware routes registered in app.py.
/* global React, TradingCore, echarts, LightweightCharts */
const { useState: _s, useEffect: _e, useRef: _r, useCallback: _cb } = React;
const TC = window.TradingCore;

// ── Helpers ──────────────────────────────────────────────────────────────────
function _fetchJSON(url) {
  return fetch(url).then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)));
}

function _todayISO() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}

function _yn(b) { return b ? 'Y' : 'N'; }

function _fmtPct(v, d) {
  if (v == null || Number.isNaN(v)) return '—';
  return TC ? TC.fmtPct(v, d == null ? 2 : d) : `${(v * 100).toFixed(d || 2)}%`;
}

function _fmtNum(v, d) {
  if (v == null || Number.isNaN(v)) return '—';
  return TC ? TC.fmtNum(v, d == null ? 2 : d) : Number(v).toFixed(d || 2);
}

function _tsShort(ts) {
  if (!ts) return '—';
  return ts.length >= 16 ? ts.slice(11, 16) : ts;
}

// ── Mini Price Chart (LightweightCharts line) ───────────────────────────────
function MiniPriceChart({ instrument }) {
  const LWC = window.LightweightCharts;
  const containerRef = _r(null);
  const chartRef = _r(null);
  const seriesRef = _r(null);
  const [error, setError] = _s(null);

  // Init chart once
  _e(() => {
    if (!LWC || !containerRef.current) return;
    const cs = getComputedStyle(document.documentElement);
    const get = v => cs.getPropertyValue(v).trim();
    const chart = LWC.createChart(containerRef.current, {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: get('--fg-3') || '#8899aa', fontSize: 9 },
      grid: { vertLines: { color: 'rgba(255,255,255,0.04)' }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
      crosshair: { mode: LWC.CrosshairMode.Magnet },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      handleWheelMove: false,
      handleScroll: false,
    });
    chartRef.current = chart;
    seriesRef.current = chart.addLineSeries({
      color: '#5a9fff',
      lineWidth: 1.5,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
    });
    const ro = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        const { width, height } = containerRef.current.getBoundingClientRect();
        chartRef.current.resize(width, Math.max(80, height));
      }
    });
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; };
  }, []);

  // Load candles and update series
  _e(() => {
    if (!seriesRef.current) return;
    const IST_SEC = 19800; // +5:30 in seconds, same shift as terminal-live.jsx
    fetch(`/api/candles?instrument=${instrument}&bars=80`)
      .then(r => r.ok ? r.json() : [])
      .then(bars => {
        if (!seriesRef.current || !Array.isArray(bars) || bars.length === 0) {
          setError('no data');
          return;
        }
        // Bars from ingestion have: start_at (ISO string), open, high, low, close
        // Deduplicate by time (ingestion writes multiple sub-minute entries per bar);
        // LWC requires strictly increasing unique timestamps or setData renders nothing.
        const seen = new Map();
        for (const b of bars) {
          const iso = b.start_at || b.time || b.timestamp;
          const close = b.close || b.c || b.ltp;
          if (!iso || close == null) continue;
          const utcMs = new Date(iso).getTime();
          if (!isFinite(utcMs)) continue;
          const tSec = Math.floor(utcMs / 1000) + IST_SEC;
          seen.set(tSec, Number(close)); // last write wins (newest value per bar)
        }
        const data = Array.from(seen.entries())
          .map(([time, value]) => ({ time, value }))
          .sort((a, b) => a.time - b.time);
        if (data.length > 0) {
          seriesRef.current.setData(data);
          chartRef.current && chartRef.current.timeScale().fitContent();
          setError(null);
        } else {
          setError('no data');
        }
      })
      .catch(() => setError('no data'));
  }, [instrument]);

  if (!LWC) return React.createElement('div', { className: 'mi-minichart-empty' }, 'chart unavailable');
  return React.createElement('div', { className: 'mi-minichart-wrap' },
    React.createElement('div', { ref: containerRef, className: 'mi-minichart', style: { width: '100%', height: 90 } }),
    error && React.createElement('div', { className: 'mi-minichart-empty' }, error),
  );
}

// ── S1: Instrument Switcher + Status Bar ───────────────────────────────────
function ModeBadge({ mode }) {
  const cls = mode === 'live' ? 'mi-mode-live'
    : mode === 'paper' ? 'mi-mode-paper'
    : mode === 'sim' ? 'mi-mode-sim'
    : 'mi-mode-off';
  return React.createElement('span', { className: `mi-mode-badge ${cls}` }, (mode || 'off').toUpperCase());
}

function InstrumentSwitcher({ instruments, selected, onSelect }) {
  if (!instruments || instruments.length === 0) {
    return React.createElement('div', { className: 'mi-empty' }, 'No instruments available');
  }
  return React.createElement('div', { className: 'mi-instrument-bar' },
    instruments.map(inst => {
      const stale = inst.feed_stale || (inst.feed_last_tick_age_sec != null && inst.feed_last_tick_age_sec > 120);
      return React.createElement('div', {
        key: inst.id,
        className: `mi-instrument-card ${selected === inst.id ? 'mi-selected' : ''} ${stale ? 'mi-stale' : ''}`,
        onClick: () => onSelect(inst.id),
      },
        React.createElement('div', { className: 'mi-inst-header' },
          React.createElement('span', { className: 'mi-inst-id' }, inst.id),
          React.createElement(ModeBadge, { mode: inst.mode }),
        ),
        React.createElement(MiniPriceChart, { instrument: inst.id }),
        React.createElement('div', { className: 'mi-inst-grid' },
          React.createElement('div', { className: 'mi-kv' },
            React.createElement('span', { className: 'mi-k' }, 'Models'),
            React.createElement('span', { className: 'mi-v' },
              `E:${_yn(inst.model_entry_loaded)} D:${_yn(inst.model_direction_loaded)}`
            ),
          ),
          React.createElement('div', { className: 'mi-kv' },
            React.createElement('span', { className: 'mi-k' }, 'Trades'),
            React.createElement('span', { className: 'mi-v' }, String(inst.today_trades || 0)),
          ),
          React.createElement('div', { className: 'mi-kv' },
            React.createElement('span', { className: 'mi-k' }, 'P&L'),
            React.createElement('span', { className: `mi-v ${(inst.today_pnl_pct || 0) >= 0 ? 'mi-pos' : 'mi-neg'}` },
              _fmtPct(inst.today_pnl_pct),
            ),
          ),
          React.createElement('div', { className: 'mi-kv' },
            React.createElement('span', { className: 'mi-k' }, 'Feed'),
            React.createElement('span', { className: `mi-v ${stale ? 'mi-stale' : ''}` },
              inst.feed_last_tick_age_sec != null ? `${inst.feed_last_tick_age_sec}s` : '—',
            ),
          ),
          React.createElement('div', { className: 'mi-kv' },
            React.createElement('span', { className: 'mi-k' }, 'Regime'),
            React.createElement('span', { className: 'mi-v' }, inst.regime || '—'),
          ),
          // S6: Expiry context embedded
          React.createElement('div', { className: 'mi-kv mi-expiry' },
            React.createElement('span', { className: 'mi-k' }, 'Expiry'),
            React.createElement('span', { className: 'mi-v' },
              inst.current_expiry || '—',
              inst.dte != null && inst.dte > 0 ? ` (${inst.dte}d)` : '',
              inst.dte === 0 ? ' (TODAY)' : '',
            ),
          ),
        ),
      );
    })
  );
}

// ── S2: Effective Config Panel ─────────────────────────────────────────────
function EffectiveConfigPanel({ instrument }) {
  const [config, setConfig] = _s(null);
  const [error, setError] = _s(null);
  const [loading, setLoading] = _s(true);

  _e(() => {
    setLoading(true);
    _fetchJSON(`/api/config/effective?instrument=${instrument}&mode=live`)
      .then(d => { setConfig(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [instrument]);

  if (loading) return React.createElement('div', { className: 'mi-panel-loading' }, 'Loading config…');
  if (error) return React.createElement('div', { className: 'mi-panel-error' }, `Error: ${error}`);
  if (!config) return null;

  const entry = config.entry_model || {};
  const direction = config.direction_model || {};

  const rows = [
    ['Engine', config.engine],
    ['Profile', config.strategy_profile_id],
    ['Entry Model Path', entry.path || '—'],
    ['Entry Model Exists', _yn(entry.exists)],
    ['Entry ML Min Prob', _fmtNum(config.entry_ml_min_prob, 4)],
    ['Direction Mode', config.direction_mode],
    ['Direction Model Path', direction.path || '—'],
    ['Direction Model Exists', _yn(direction.exists)],
    ['Strategy Min Confidence', _fmtNum(config.strategy_min_confidence, 4)],
    ['Exit Strategy Mode', config.exit_strategy_mode],
    ['Exit Max Loss %', _fmtPct(config.exit_max_loss_pct)],
    ['Entry Time Windows', config.entry_time_windows],
    ['Rollout Stage', config.rollout_stage || '—'],
    ['Runtime Config Started', config.runtime_config_started_at || '—'],
    ['Checked At', config.checked_at_ist || '—'],
  ];

  return React.createElement('div', { className: 'mi-config-panel' },
    React.createElement('div', { className: 'mi-panel-title' }, 'Effective Config (Live Process)'),
    React.createElement('table', { className: 'mi-config-table' },
      React.createElement('tbody', null,
        rows.map(([k, v]) =>
          React.createElement('tr', { key: k },
            React.createElement('td', { className: 'mi-config-key' }, k),
            React.createElement('td', { className: 'mi-config-val' }, String(v)),
          )
        )
      )
    )
  );
}

// ── S3: Model Health Panel ─────────────────────────────────────────────────
function ModelHealthPanel({ instrument }) {
  const [health, setHealth] = _s(null);
  const [error, setError] = _s(null);
  const [loading, setLoading] = _s(true);
  const chartRef = _r(null);
  const chartInstance = _r(null);

  _e(() => {
    setLoading(true);
    _fetchJSON(`/api/model-health?instrument=${instrument}&lookback_days=5`)
      .then(d => { setHealth(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [instrument]);

  _e(() => {
    if (!health || !chartRef.current || typeof echarts === 'undefined') return;
    if (chartInstance.current) chartInstance.current.dispose();
    chartInstance.current = echarts.init(chartRef.current);
    const hist = health.entry_model && health.entry_model.prob_histogram ? health.entry_model.prob_histogram : [];
    const labels = hist.map(h => h.bucket);
    const counts = hist.map(h => h.count);
    const threshold = (health.entry_model && health.entry_model.threshold) || 0;
    const option = {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, top: 30, bottom: 40 },
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 10, color: '#8899aa' } },
      yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#8899aa' } },
      series: [
        {
          type: 'bar',
          data: counts,
          itemStyle: { color: '#5a9fff' },
        },
        {
          type: 'line',
          markLine: {
            silent: true,
            data: [{ xAxis: threshold >= 0.80 ? 11 : threshold >= 0.70 ? 10 : threshold >= 0.60 ? 9 : threshold >= 0.50 ? 8 : threshold >= 0.40 ? 7 : threshold >= 0.30 ? 6 : threshold >= 0.25 ? 5 : threshold >= 0.20 ? 4 : threshold >= 0.15 ? 3 : threshold >= 0.10 ? 2 : threshold >= 0.05 ? 1 : 0 }],
            lineStyle: { color: '#ff6b6b', type: 'dashed', width: 2 },
            label: { formatter: `threshold=${threshold}`, fontSize: 10, color: '#ff6b6b' },
          },
        },
      ],
    };
    chartInstance.current.setOption(option);
    const resize = () => chartInstance.current && chartInstance.current.resize();
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); chartInstance.current && chartInstance.current.dispose(); };
  }, [health]);

  if (loading) return React.createElement('div', { className: 'mi-panel-loading' }, 'Loading model health…');
  if (error) return React.createElement('div', { className: 'mi-panel-error' }, `Error: ${error}`);
  if (!health) return null;

  const em = health.entry_model || {};
  const dm = health.direction_model || {};

  return React.createElement('div', { className: 'mi-health-panel' },
    React.createElement('div', { className: 'mi-panel-title' }, 'Model Health'),
    React.createElement('div', { className: 'mi-health-grid' },
      React.createElement('div', { className: 'mi-health-section' },
        React.createElement('div', { className: 'mi-health-subtitle' },
          `Entry: ${em.path ? em.path.split('/').pop() : '—'}`,
        ),
        React.createElement('div', { className: 'mi-health-stats' },
          React.createElement('span', null, `threshold: ${_fmtNum(em.threshold, 3)}`),
          React.createElement('span', null, `prob[min/mean/p50/max]: ${_fmtNum(em.prob_min, 3)}/${_fmtNum(em.prob_mean, 3)}/${_fmtNum(em.prob_p50, 3)}/${_fmtNum(em.prob_max, 3)}`),
          React.createElement('span', { className: 'mi-health-pass' },
            `pass: ${em.bars_above_threshold || 0}/${em.bars_total || 0} (${em.bars_above_threshold_pct || 0}%)`,
          ),
          em.degenerate_zero_entries && React.createElement('span', { className: 'mi-neg' }, 'DEGENERATE: max prob below threshold'),
        ),
        React.createElement('div', { ref: chartRef, className: 'mi-histogram-chart' }),
      ),
      React.createElement('div', { className: 'mi-health-section' },
        React.createElement('div', { className: 'mi-health-subtitle' },
          `Direction: ${dm.path ? dm.path.split('/').pop() : '—'}`,
        ),
        React.createElement('div', { className: 'mi-health-stats' },
          React.createElement('span', null, `CE mean: ${dm.ce_prob_mean != null ? _fmtNum(dm.ce_prob_mean, 3) : '—'}`),
          React.createElement('span', null, `PE mean: ${dm.pe_prob_mean != null ? _fmtNum(dm.pe_prob_mean, 3) : '—'}`),
          React.createElement('span', null, `n bars: ${dm.n_direction_bars || 0}`),
        ),
      ),
    ),
  );
}

// ── S4: Decision Trace Viewer ──────────────────────────────────────────────
function DecisionTraceViewer({ instrument }) {
  const [signals, setSignals] = _s([]);
  const [summary, setSummary] = _s(null);
  const [error, setError] = _s(null);
  const [loading, setLoading] = _s(true);
  const [date, setDate] = _s(_todayISO());
  const [outcomeFilter, setOutcomeFilter] = _s('');
  const [expandedRow, setExpandedRow] = _s(null);
  const [wsStatus, setWsStatus] = _s('disconnected');
  const [liveSignals, setLiveSignals] = _s([]);
  const wsRef = _r(null);

  const loadData = _cb(() => {
    setLoading(true);
    const params = new URLSearchParams({ instrument, date });
    if (outcomeFilter) params.set('outcome', outcomeFilter);
    _fetchJSON(`/api/signals?${params.toString()}`)
      .then(d => { setSignals(d.signals || []); setSummary(d.summary || null); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [instrument, date, outcomeFilter]);

  _e(() => { loadData(); }, [loadData]);

  // WebSocket for live feed
  _e(() => {
    let alive = true;
    let retryId = null;
    const connect = () => {
      if (!alive) return;
      try {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${proto}://${location.host}/ws/signals?instrument=${instrument}`);
        wsRef.current = ws;
        ws.onopen = () => setWsStatus('connected');
        ws.onclose = () => { setWsStatus('disconnected'); if (alive) retryId = setTimeout(connect, 5000); };
        ws.onerror = () => setWsStatus('error');
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'signal') {
              setLiveSignals(prev => [...prev.slice(-100), msg]);
            }
          } catch (_) {}
        };
      } catch (_) { setWsStatus('error'); if (alive) retryId = setTimeout(connect, 5000); }
    };
    connect();
    return () => { alive = false; clearTimeout(retryId); if (wsRef.current) wsRef.current.close(); };
  }, [instrument]);

  const allSignals = [...liveSignals, ...signals];

  if (loading) return React.createElement('div', { className: 'mi-panel-loading' }, 'Loading signals…');
  if (error) return React.createElement('div', { className: 'mi-panel-error' }, `Error: ${error}`);

  return React.createElement('div', { className: 'mi-signals-panel' },
    React.createElement('div', { className: 'mi-panel-title' },
      'Decision Trace',
      React.createElement('span', { className: `mi-ws-status mi-ws-${wsStatus}` }, `WS: ${wsStatus}`),
    ),
    React.createElement('div', { className: 'mi-filter-bar' },
      React.createElement('label', { className: 'mi-date-label' }, 'Date: ',
        React.createElement('input', { type: 'date', value: date, onChange: e => setDate(e.target.value) }),
      ),
      React.createElement('button', { className: `mi-filter-btn ${outcomeFilter === '' ? 'active' : ''}`, onClick: () => setOutcomeFilter('') }, 'All'),
      React.createElement('button', { className: `mi-filter-btn ${outcomeFilter === 'entry_taken' ? 'active' : ''}`, onClick: () => setOutcomeFilter('entry_taken') }, 'Taken'),
      React.createElement('button', { className: `mi-filter-btn ${outcomeFilter === 'blocked' ? 'active' : ''}`, onClick: () => setOutcomeFilter('blocked') }, 'Blocked'),
      React.createElement('button', { className: `mi-filter-btn ${outcomeFilter === 'hold' ? 'active' : ''}`, onClick: () => setOutcomeFilter('hold') }, 'Hold'),
      React.createElement('button', { className: `mi-filter-btn ${outcomeFilter === 'manage_only' ? 'active' : ''}`, onClick: () => setOutcomeFilter('manage_only') }, 'Manage'),
    ),
    summary && React.createElement('div', { className: 'mi-signals-summary' },
      React.createElement('span', null, `Filtered: ${summary.total_filtered || 0}`),
      React.createElement('span', { className: 'mi-pos' }, `Taken: ${summary.entry_taken || 0}`),
      React.createElement('span', { className: 'mi-neg' }, `Blocked: ${summary.blocked || 0}`),
      React.createElement('span', null, `Hold: ${summary.hold || 0}`),
      React.createElement('span', null, `Manage: ${summary.manage_only || 0}`),
    ),
    summary && summary.blocked_by_gate && React.createElement('div', { className: 'mi-signals-summary' },
      Object.entries(summary.blocked_by_gate).map(([gate, count]) =>
        React.createElement('span', { key: gate, className: 'mi-neg' }, `${gate}: ${count}`)
      ),
    ),
    React.createElement('div', { className: 'mi-signals-list' },
      allSignals.length === 0
        ? React.createElement('div', { className: 'mi-empty' }, 'No signals for this date')
        : allSignals.slice(-200).reverse().map((sig, i) =>
            React.createElement('div', { key: i, className: `mi-signal-row mi-outcome-${(sig.outcome || '').toLowerCase()}` },
              React.createElement('div', {
                className: 'mi-signal-main',
                onClick: () => setExpandedRow(expandedRow === i ? null : i),
              },
                React.createElement('span', { className: 'mi-sig-time' }, _tsShort(sig.ts)),
                React.createElement('span', { className: 'mi-sig-prob' }, `p=${_fmtNum(sig.entry_prob, 3)}`),
                React.createElement('span', { className: 'mi-sig-dir' }, sig.direction || '—'),
                React.createElement('span', { className: 'mi-sig-conf' }, `conf=${_fmtNum(sig.direction_conf, 3)}`),
                React.createElement('span', { className: `mi-sig-outcome mi-outcome-${(sig.outcome || '').toLowerCase()}` }, sig.outcome || '—'),
                sig.block_reason && React.createElement('span', { className: 'mi-sig-block' }, sig.block_reason),
              ),
              expandedRow === i && React.createElement('div', { className: 'mi-signal-expanded' },
                React.createElement('div', { className: 'mi-gates-grid' },
                  Object.entries(sig.gates || {}).map(([gate, passed]) =>
                    React.createElement('span', {
                      key: gate,
                      className: `mi-gate ${passed ? 'mi-gate-pass' : 'mi-gate-fail'}`,
                    }, `${gate}: ${passed ? '✓' : '✗'}`)
                  ),
                ),
                React.createElement('pre', { className: 'mi-raw-trace' }, JSON.stringify(sig, null, 2)),
              ),
            )
          )
    ),
  );
}

// ── S5: Trade P&L Timeline ─────────────────────────────────────────────────
function TradePnLTimeline({ instrument }) {
  const [data, setData] = _s(null);
  const [error, setError] = _s(null);
  const [loading, setLoading] = _s(true);
  const [fromDate, setFromDate] = _s(_todayISO());
  const [toDate, setToDate] = _s(_todayISO());
  const chartRef = _r(null);
  const chartInstance = _r(null);

  _e(() => {
    setLoading(true);
    const params = new URLSearchParams({ instrument, from: fromDate, to: toDate });
    _fetchJSON(`/api/trades?${params.toString()}`)
      .then(d => { setData(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [instrument, fromDate, toDate]);

  _e(() => {
    if (!data || !data.trades || !chartRef.current || typeof echarts === 'undefined') return;
    if (chartInstance.current) chartInstance.current.dispose();
    chartInstance.current = echarts.init(chartRef.current);
    const trades = data.trades;
    const times = [...new Set(trades.map(t => t.entry_time || t.date || ''))].sort();
    const scatterData = trades.map(t => ({
      value: [t.entry_time || t.date, t.pnl_pct || 0, t.entry_prob || 0, t.side],
    }));
    const option = {
      tooltip: {
        trigger: 'item',
        formatter: (p) => {
          const v = p.value;
          return `Time: ${v[0]}<br/>P&L: ${TC ? TC.fmtPct(v[1], 2) : _fmtPct(v[1], 2)}<br/>Entry Prob: ${_fmtNum(v[2], 3)}<br/>Side: ${v[3]}`;
        },
      },
      grid: { left: 60, right: 20, top: 20, bottom: 40 },
      xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10, color: '#8899aa' } },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: '#8899aa', formatter: v => (v * 100).toFixed(1) + '%' },
      },
      series: [{
        type: 'scatter',
        data: scatterData,
        symbolSize: (val) => Math.max(6, (val[2] || 0) * 30),
        itemStyle: {
          color: (p) => p.value[3] === 'CE' ? '#5a9fff' : '#ff8c5a',
        },
      }],
    };
    chartInstance.current.setOption(option);
    const resize = () => chartInstance.current && chartInstance.current.resize();
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); chartInstance.current && chartInstance.current.dispose(); };
  }, [data]);

  if (loading) return React.createElement('div', { className: 'mi-panel-loading' }, 'Loading trades…');
  if (error) return React.createElement('div', { className: 'mi-panel-error' }, `Error: ${error}`);
  if (!data) return null;

  const s = data.summary || {};

  return React.createElement('div', { className: 'mi-trades-panel' },
    React.createElement('div', { className: 'mi-panel-title' }, 'Trade P&L Timeline'),
    React.createElement('div', { className: 'mi-date-filters' },
      React.createElement('label', null, 'From: ',
        React.createElement('input', { type: 'date', value: fromDate, onChange: e => setFromDate(e.target.value) }),
      ),
      React.createElement('label', null, 'To: ',
        React.createElement('input', { type: 'date', value: toDate, onChange: e => setToDate(e.target.value) }),
      ),
    ),
    React.createElement('div', { className: 'mi-trade-summary' },
      React.createElement('span', null, `Trades: ${s.count || 0}`),
      React.createElement('span', null, `Win Rate: ${s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—'}`),
      React.createElement('span', null, `Avg P&L: ${_fmtPct(s.avg_pnl_pct)}`),
      React.createElement('span', null, `Avg Entry Prob: ${_fmtNum(s.avg_entry_prob, 3)}`),
      React.createElement('span', null, `Avg Dir Conf: ${_fmtNum(s.avg_direction_conf, 3)}`),
      React.createElement('span', null, `CE: ${s.ce_count || 0} PE: ${s.pe_count || 0}`),
    ),
    React.createElement('div', { ref: chartRef, className: 'mi-scatter-chart' }),
    React.createElement('table', { className: 'mi-trades-table' },
      React.createElement('thead', null,
        React.createElement('tr', null,
          ['Date', 'Entry', 'Side', 'Strike', 'Prob', 'Dir Conf', 'Regime', 'Entry ₹', 'Exit ₹', 'P&L%', 'Exit Reason', 'Hold'].map(h =>
            React.createElement('th', { key: h }, h),
          ),
        ),
      ),
      React.createElement('tbody', null,
        (data.trades || []).map(t =>
          React.createElement('tr', { key: t.trade_id },
            React.createElement('td', null, t.date),
            React.createElement('td', null, t.entry_time),
            React.createElement('td', { className: t.side === 'CE' ? 'mi-ce' : 'mi-pe' }, t.side),
            React.createElement('td', null, t.strike),
            React.createElement('td', null, _fmtNum(t.entry_prob, 3)),
            React.createElement('td', null, _fmtNum(t.direction_conf, 3)),
            React.createElement('td', null, t.regime),
            React.createElement('td', null, _fmtNum(t.entry_price, 1)),
            React.createElement('td', null, _fmtNum(t.exit_price, 1)),
            React.createElement('td', { className: (t.pnl_pct || 0) >= 0 ? 'mi-pos' : 'mi-neg' }, _fmtPct(t.pnl_pct)),
            React.createElement('td', null, t.exit_reason),
            React.createElement('td', null, `${t.hold_minutes || 0}m`),
          )
        ),
      ),
    ),
  );
}

// ── Replay Panel ────────────────────────────────────────────────────────────
function ReplayPanel({ instrument }) {
  const [date, setDate] = _s(() => {
    const d = new Date(); d.setDate(d.getDate() - 1);
    return d.toISOString().slice(0,10);
  });
  const [speed, setSpeed] = _s(300);
  const [run, setRun] = _s(null);
  const [error, setError] = _s(null);
  const [runs, setRuns] = _s([]);

  // Poll current run status
  _e(() => {
    if (!run || run.status === 'done' || run.status === 'error' || run.status === 'cancelled') return;
    const id = setInterval(() => {
      _fetchJSON(`/api/replay/${run.run_id}`)
        .then(r => setRun(r))
        .catch(() => {});
    }, 1500);
    return () => clearInterval(id);
  }, [run]);

  // Load recent runs
  const loadRuns = _cb(() => {
    _fetchJSON(`/api/replay/runs?instrument=${instrument}&limit=8`)
      .then(r => setRuns(r)).catch(() => {});
  }, [instrument]);

  _e(() => { loadRuns(); }, [loadRuns]);

  const startReplay = () => {
    setError(null);
    setRun(null);
    fetch('/api/replay/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ date, instrument, speed: parseFloat(speed) }),
    })
    .then(r => r.json())
    .then(r => { if (r.run_id) setRun(r); else setError(r.detail || 'Failed'); loadRuns(); })
    .catch(e => setError(e.message));
  };

  const cancelReplay = () => {
    if (!run) return;
    fetch(`/api/replay/${run.run_id}`, { method: 'DELETE' })
      .then(r => r.json()).then(r => setRun(prev => ({...prev, ...r}))).catch(() => {});
  };

  const statusColor = (s) => ({
    done:'#4caf50', error:'#f44336', running:'#ffd740', cancelled:'#888', queued:'#888', fetching:'#29b6f6', building:'#ab47bc', replaying:'#ffd740',
  }[s] || '#888');

  const pct = run ? Math.round(100*(run.emitted||0)/Math.max(1,run.total||1)) : 0;

  return React.createElement('div', { className: 'mi-panel mi-replay-panel' },
    React.createElement('div', { className: 'mi-panel-title' }, '▶ Replay from Dhan'),
    React.createElement('div', { className: 'mi-replay-controls' },
      React.createElement('label', null, 'Date ',
        React.createElement('input', { type:'date', value:date, onChange:e=>setDate(e.target.value), className:'mi-replay-date' }),
      ),
      React.createElement('label', null, ' Speed ',
        React.createElement('select', { value:speed, onChange:e=>setSpeed(e.target.value), className:'mi-replay-speed' },
          React.createElement('option', {value:50}, '50× (slow)'),
          React.createElement('option', {value:150}, '150×'),
          React.createElement('option', {value:300}, '300× (~75s)'),
          React.createElement('option', {value:600}, '600× (fast)'),
        ),
      ),
      React.createElement('button', {
        className: 'mi-replay-btn',
        onClick: startReplay,
        disabled: !!(run && ['queued','fetching','building','replaying'].includes(run.status)),
      }, '▶ Run Replay'),
      run && !['done','error','cancelled'].includes(run.status) &&
        React.createElement('button', { className:'mi-replay-cancel', onClick:cancelReplay }, '✕ Cancel'),
    ),
    error && React.createElement('div', { className:'mi-panel-error' }, error),
    run && React.createElement('div', { className:'mi-replay-status' },
      React.createElement('span', { style:{color:statusColor(run.status), fontWeight:'bold'} },
        (run.status||'').toUpperCase(),
      ),
      ' — ', run.message || '',
      (run.total > 0) && React.createElement('div', { className:'mi-replay-progress' },
        React.createElement('div', { className:'mi-replay-bar', style:{width:`${pct}%`} }),
      ),
      (run.total > 0) && React.createElement('span', { className:'mi-replay-pct' }, `${pct}% (${run.emitted||0}/${run.total} bars)`),
      run.status === 'done' && React.createElement('div', { className:'mi-replay-done' },
        '✓ Done — check Signals and Trades panels above for results',
        React.createElement('br'),
        React.createElement('small', null,
          `Results tagged run_id: ${run.run_id} — filter in Signals panel`,
        ),
      ),
    ),
    runs.length > 0 && React.createElement('div', { className:'mi-replay-history' },
      React.createElement('div', { className:'mi-replay-history-title' }, 'Recent replays'),
      React.createElement('table', { className:'mi-replay-table' },
        React.createElement('thead', null,
          React.createElement('tr', null,
            ['Date', 'Speed', 'Status', 'Bars', 'Started'].map(h => React.createElement('th',{key:h},h)),
          ),
        ),
        React.createElement('tbody', null,
          runs.map(r => React.createElement('tr', { key:r.run_id },
            React.createElement('td', null, r.date),
            React.createElement('td', null, `${r.speed}×`),
            React.createElement('td', { style:{color:statusColor(r.status)} }, r.status),
            React.createElement('td', null, r.total || '—'),
            React.createElement('td', null, r.created_at ? r.created_at.slice(11,16) : '—'),
          )),
        ),
      ),
    ),
  );
}

// ── Main: MultiInstrumentDashboard ─────────────────────────────────────────
function MultiInstrumentDashboard() {
  const [instruments, setInstruments] = _s([]);
  const [selected, setSelected] = _s('BANKNIFTY');
  const [instError, setInstError] = _s(null);

  const loadInstruments = _cb(() => {
    _fetchJSON('/api/instruments')
      .then(d => {
        setInstruments(d);
        setInstError(null);
        if (d.length > 0 && !d.find(i => i.id === selected)) {
          setSelected(d[0].id);
        }
      })
      .catch(e => setInstError(e.message));
  }, [selected]);

  _e(() => {
    loadInstruments();
    const id = setInterval(loadInstruments, 10000);
    return () => clearInterval(id);
  }, [loadInstruments]);

  return React.createElement('div', { className: 'mi-dashboard' },
    instError && React.createElement('div', { className: 'mi-panel-error' }, `Instruments error: ${instError}`),
    React.createElement(InstrumentSwitcher, { instruments, selected, onSelect: setSelected }),
    React.createElement('div', { className: 'mi-panels-grid' },
      React.createElement(EffectiveConfigPanel, { instrument: selected }),
      React.createElement(ModelHealthPanel, { instrument: selected }),
      React.createElement(DecisionTraceViewer, { instrument: selected }),
      React.createElement(TradePnLTimeline, { instrument: selected }),
    ),
    React.createElement(ReplayPanel, { instrument: selected }),
  );
}

Object.assign(window, { MultiInstrumentDashboard });
