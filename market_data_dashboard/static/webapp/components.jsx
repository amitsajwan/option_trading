// Shared components: KpiStrip, LWChart, EChartPanel, PaginatedTable, ConfirmModal  v11
/* global React */
const { useState, useEffect, useRef, useMemo, useCallback } = React;
const TC = window.TradingCore;

const _isDark = () => document.body.classList.contains('app-dark') || document.body.classList.contains('live-terminal');

// ── KPI STRIP ────────────────────────────────────────────────────────────
function KpiStrip({ items, cols }) {
  return (
    <div className="kpi-strip" style={{ '--cols': cols || items.length }}>
      {items.map((it, i) => (
        <div className="kpi" key={i}>
          <div className="kpi-label">
            {it.label}
            {it.badge}
          </div>
          <div className={`kpi-value ${it.cls || ''}`}>{it.value}</div>
          <div className="kpi-sub">{it.sub}</div>
        </div>
      ))}
    </div>
  );
}

// ── LW CHART (Lightweight Charts v4) ─────────────────────────────────────
// Zoom/pan natively, click row→navigate, fullscreen expand, IST time axis.
function LWChart({
  candles, upToIdx, trades, signals,
  selectedTrade, selectedSignal,
  onSelectTrade, onSelectSignal,
  height, expanded, onExpandChange,
  fitRef,   // optional: caller passes a ref; we store fitContent fn on it
  isPlaying,
}) {
  const containerRef = useRef(null);
  const chartRef     = useRef(null);
  const seriesRef    = useRef(null);
  const volSeriesRef = useRef(null);
  const prevUpToRef  = useRef(-1);
  const prevCandRef  = useRef(null);
  const priceLinesRef = useRef([]);
  const connSeriesRef = useRef([]);
  const isPlayingRef = useRef(isPlaying);
  // stable refs so click handler isn't a stale closure
  const tradesRef    = useRef(trades);
  const candlesRef   = useRef(candles);
  useEffect(() => { tradesRef.current  = trades;    }, [trades]);
  useEffect(() => { candlesRef.current = candles;   }, [candles]);
  useEffect(() => { isPlayingRef.current = isPlaying; }, [isPlaying]);

  const toLW    = (c) => ({ time: Math.floor(c.t / 1000), open: c.o, high: c.h, low: c.l, close: c.c });
  const toVolLW = (c) => ({ time: Math.floor(c.t / 1000), value: c.v || 0, color: c.c >= c.o ? 'rgba(10,143,92,0.25)' : 'rgba(194,62,47,0.25)' });

  // ── Create chart once ──────────────────────────────────────────────────
  useEffect(() => {
    const LC = window.LightweightCharts;
    if (!containerRef.current || !LC) return;

    const dark = _isDark();
    const chart = LC.createChart(containerRef.current, {
      layout: {
        background: { color: dark ? '#0f1116' : '#F6F4EF' },
        textColor:  dark ? '#7d8593' : '#5A6674',
        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: dark ? 'rgba(255,255,255,0.04)' : 'rgba(11,15,20,0.05)' },
        horzLines: { color: dark ? 'rgba(255,255,255,0.04)' : 'rgba(11,15,20,0.05)' },
      },
      crosshair: { mode: 1 },   // 1 = Normal
      rightPriceScale: { borderColor: dark ? 'rgba(255,255,255,0.10)' : 'rgba(11,15,20,0.12)' },
      timeScale: {
        borderColor: dark ? 'rgba(255,255,255,0.10)' : 'rgba(11,15,20,0.12)',
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (ts) => {
          const d = new Date(ts * 1000);
          const hh = String(d.getUTCHours() + 5).padStart(2, '0');
          const raw = d.getUTCMinutes() + 30;
          const mm  = String(raw >= 60 ? raw - 60 : raw).padStart(2, '0');
          const hAdj = raw >= 60 ? String(d.getUTCHours() + 6).padStart(2, '0') : hh;
          return `${hAdj}:${mm}`;
        },
      },
      localization: {
        timeFormatter: (ts) => {
          const d = new Date(ts * 1000);
          return d.toLocaleTimeString('en-IN', {
            timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false,
          });
        },
      },
    });

    const upCol   = dark ? '#19c37d' : '#0A8F5C';
    const downCol = dark ? '#f23c4a' : '#C23E2F';
    const series = chart.addCandlestickSeries({
      upColor:         upCol,
      downColor:       downCol,
      borderUpColor:   upCol,
      borderDownColor: downCol,
      wickUpColor:     upCol,
      wickDownColor:   downCol,
      priceLineVisible: false,
    });

    const volSeries = chart.addHistogramSeries({
      priceScaleId: 'vol',
      priceFormat: { type: 'volume' },
      color: 'rgba(11,15,20,0.15)',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    // Click on chart → select nearest trade
    chart.subscribeClick((param) => {
      if (!param.time) return;
      const clickMs = param.time * 1000;
      const allTrades  = tradesRef.current  || [];
      const allCandles = candlesRef.current || [];
      let best = null, bestDist = Infinity;
      for (const tr of allTrades) {
        const de = Math.abs(tr.t - clickMs);
        if (de < bestDist && de < 90000) { best = tr; bestDist = de; }
        const exitC = allCandles[tr.exitIdx];
        if (exitC) {
          const dx = Math.abs(exitC.t - clickMs);
          if (dx < bestDist && dx < 90000) { best = tr; bestDist = dx; }
        }
      }
      if (best) onSelectTrade && onSelectTrade(best);
    });

    // Auto-resize when container changes size
    const ro = new ResizeObserver(() => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.resize(
        containerRef.current.clientWidth,
        containerRef.current.clientHeight || (height || 340),
      );
    });
    ro.observe(containerRef.current);

    chartRef.current     = chart;
    seriesRef.current    = series;
    volSeriesRef.current = volSeries;
    if (fitRef) fitRef.current = () => chart.timeScale().fitContent();
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; volSeriesRef.current = null; };
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // ── Feed candle data ───────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !candles.length) return;
    const idx = upToIdx == null ? candles.length - 1 : Math.min(upToIdx, candles.length - 1);
    const sameSession = prevCandRef.current === candles;

    if (sameSession && idx === prevUpToRef.current + 1 && prevUpToRef.current >= 0) {
      // single-bar replay increment → fast path
      seriesRef.current.update(toLW(candles[idx]));
      if (volSeriesRef.current) volSeriesRef.current.update(toVolLW(candles[idx]));
    } else {
      // session change or jump → full reset
      // Only preserve visible range when NOT playing (seek, scrub, session load)
      const saved = (!isPlayingRef.current && sameSession && chartRef.current)
        ? chartRef.current.timeScale().getVisibleRange() : null;
      const slice = candles.slice(0, idx + 1);
      seriesRef.current.setData(slice.map(toLW));
      if (volSeriesRef.current) volSeriesRef.current.setData(slice.map(toVolLW));
      if (saved) {
        try { chartRef.current.timeScale().setVisibleRange(saved); } catch (_) {
          chartRef.current.timeScale().fitContent();
        }
      } else {
        // New session (playing or not): anchor a ~90-bar window from the left
        // so streaming bars fill the chart instead of being squeezed to the right edge.
        const SPAN = 90;
        chartRef.current.timeScale().setVisibleLogicalRange({
          from: -0.5,
          to: Math.max(SPAN, idx + 1) - 0.5,
        });
      }
    }

    // Follow latest bar whenever playing — applies to both fast and slow paths
    if (isPlayingRef.current && chartRef.current) {
      try {
        const ts = chartRef.current.timeScale();
        const vr = ts.getVisibleLogicalRange();
        if (vr !== null) {
          const span = Math.max(1, vr.to - vr.from);
          // Anchor from at 0 until enough bars have streamed in to fill the window.
          const from = Math.max(0, idx - span + 1);
          const to = Math.max(span, idx + 1);
          ts.setVisibleLogicalRange({ from: from - 0.5, to: to - 0.5 });
        } else {
          chartRef.current.timeScale().fitContent();
        }
      } catch (_) {}
    }

    prevUpToRef.current = idx;
    prevCandRef.current = candles;
  }, [candles, upToIdx]);

  // ── Rebuild markers ────────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current) return;
    const dark = _isDark();
    const visIdx = upToIdx == null ? Infinity : upToIdx;
    const markers = [];

    // Signal dots — only traded signals (skipped signals are table-only; every-bar dots add noise)
    (signals || []).forEach(sig => {
      if (!sig.traded && !(selectedSignal && selectedSignal === sig)) return;
      const isSel = selectedSignal && selectedSignal === sig;
      markers.push({
        time:     Math.floor(sig.t / 1000),
        position: 'belowBar',
        color:    sig.traded ? (dark ? '#19c37d' : '#0A8F5C') : (dark ? '#f5a524' : '#B97405'),
        shape:    'circle',
        text:     '',
        size:     isSel ? 2 : 0.5,
      });
    });

    // Trade entry / exit arrows
    (trades || []).forEach(tr => {
      if (tr.entryIdx > visIdx) return;
      const isSel      = selectedTrade && tr.id === selectedTrade.id;
      const entryColor = tr.dir === 'LONG' ? (dark ? '#19c37d' : '#0A8F5C') : (dark ? '#f23c4a' : '#C23E2F');
      const pnlColor   = tr.pnlPct >= 0   ? (dark ? '#19c37d' : '#0A8F5C') : (dark ? '#f23c4a' : '#C23E2F');
      markers.push({
        time:     Math.floor(tr.t / 1000),
        position: 'belowBar',
        color:    entryColor,
        shape:    'arrowUp',
        text:     isSel ? tr.dir : '',
        size:     isSel ? 2 : 1,
      });
      if (tr.exitIdx <= visIdx && candles[tr.exitIdx]) {
        markers.push({
          time:     Math.floor(candles[tr.exitIdx].t / 1000),
          position: 'aboveBar',
          color:    pnlColor,
          shape:    'arrowDown',
          // tr.pnlPct is a FRACTION (e.g. 0.0738 = +7.38%) — multiply by 100 for display.
          text:     isSel ? ((tr.pnlPct >= 0 ? '+' : '') + (tr.pnlPct * 100).toFixed(2) + '%') : '',
          size:     isSel ? 2 : 1,
        });
      }
    });

    markers.sort((a, b) => a.time - b.time);
    seriesRef.current.setMarkers(markers);
  }, [trades, signals, selectedTrade, selectedSignal, upToIdx, candles]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    try { connSeriesRef.current.forEach(s => { try { chart.removeSeries(s); } catch (_) {} }); } catch (_) {}
    connSeriesRef.current = [];
    const dark = _isDark();
    const visIdx = upToIdx == null ? Infinity : upToIdx;
    const list = Array.isArray(trades) ? trades : [];
    for (const tr of list) {
      if (tr.entryIdx > visIdx) continue;
      const cEntry = candles[tr.entryIdx];
      const cExit  = tr.exitIdx != null && tr.exitIdx <= visIdx ? candles[tr.exitIdx] : null;
      const entryTs = cEntry ? Math.floor(cEntry.t / 1000) : (tr.t ? Math.floor(tr.t / 1000) : null);
      const exitTs  = cExit  ? Math.floor(cExit.t  / 1000) : entryTs;
      const entryPx = cEntry ? cEntry.c : (tr.entryPx ?? tr.entry);
      const exitPx  = cExit  ? cExit.c  : (tr.exitPx  ?? tr.exit  ?? entryPx);
      if (entryTs == null || entryPx == null || exitTs == null || exitPx == null) continue;
      const color = tr.pnlPct > 0 ? (dark ? '#19c37d' : '#0A8F5C') : tr.pnlPct < 0 ? (dark ? '#f23c4a' : '#C23E2F') : (dark ? '#7d8593' : '#5A6674');
      const s = chart.addLineSeries({ color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
      s.setData([{ time: entryTs, value: entryPx }, { time: exitTs, value: exitPx }]);
      connSeriesRef.current.push(s);
    }
  }, [trades, candles, upToIdx]);

  // ── Price lines for selected trade (entry / stop / target) ────────────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    // Clear previous lines
    priceLinesRef.current.forEach(l => { try { series.removePriceLine(l); } catch (_) {} });
    priceLinesRef.current = [];
    if (!selectedTrade) return;

    const tr = selectedTrade;
    const dir = tr.dir || tr.direction || 'LONG';
    const entryPx = tr.entryPx ?? tr.entry ?? null;
    if (entryPx == null || !isFinite(entryPx)) return;

    let stopPx   = tr.stopPx   ?? tr.stop_price   ?? tr.exit_stop_price ?? null;
    let targetPx = tr.targetPx ?? tr.target_price ?? null;
    if (stopPx == null && typeof tr.stop_loss_pct === 'number') {
      stopPx = dir === 'LONG' ? entryPx * (1 - tr.stop_loss_pct) : entryPx * (1 + tr.stop_loss_pct);
    }
    if (targetPx == null && typeof tr.target_pct === 'number') {
      targetPx = dir === 'LONG' ? entryPx * (1 + tr.target_pct) : entryPx * (1 - tr.target_pct);
    }

    const mkLine = (price, color, style, title) => {
      try {
        const pl = series.createPriceLine({
          price, color, lineWidth: 1, lineStyle: style,
          axisLabelVisible: true, title,
        });
        priceLinesRef.current.push(pl);
      } catch (_) {}
    };
    mkLine(entryPx, '#ffb000', 0, `Entry ${entryPx.toFixed(2)}`);
    if (stopPx   != null && isFinite(stopPx))   mkLine(stopPx,   '#f23c4a', 2, `Stop ${stopPx.toFixed(2)}`);
    if (targetPx != null && isFinite(targetPx)) mkLine(targetPx, '#19c37d', 2, `Target ${targetPx.toFixed(2)}`);

    return () => {
      priceLinesRef.current.forEach(l => { try { series.removePriceLine(l); } catch (_) {} });
      priceLinesRef.current = [];
    };
  }, [selectedTrade]);

  // ── Navigate when selection changes ───────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return;
    const firstTs = candles.length ? Math.floor(candles[0].t / 1000) : 0;
    const lastTs  = candles.length ? Math.floor(candles[Math.min(upToIdx ?? candles.length - 1, candles.length - 1)].t / 1000) : 0;
    if (selectedTrade) {
      const entryTs = Math.floor(selectedTrade.t / 1000);
      const exitC   = candles[selectedTrade.exitIdx];
      const exitTs  = exitC ? Math.floor(exitC.t / 1000) : entryTs + 1800;
      const span    = Math.max(exitTs - entryTs, 300);
      const buf     = Math.round(span * 0.5);
      const from    = Math.max(entryTs - buf, firstTs);
      const to      = Math.min(exitTs + buf, lastTs + 300);
      try { chartRef.current.timeScale().setVisibleRange({ from, to }); } catch (_) {}
    } else if (selectedSignal) {
      const ts   = Math.floor(selectedSignal.t / 1000);
      const from = Math.max(ts - 600, firstTs);
      const to   = Math.min(ts + 900, lastTs + 300);
      try { chartRef.current.timeScale().setVisibleRange({ from, to }); } catch (_) {}
    }
  }, [selectedTrade, selectedSignal]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ── Escape key to exit fullscreen ──────────────────────────────────────
  useEffect(() => {
    if (!expanded) return;
    const fn = (e) => { if (e.key === 'Escape') onExpandChange && onExpandChange(false); };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [expanded, onExpandChange]);

  const h = expanded ? '100%' : (height || 340);
  return <div ref={containerRef} style={{ width: '100%', height: h, minHeight: 200 }} />;
}

// ── ECHART PANEL ─────────────────────────────────────────────────────────
// Thin ECharts wrapper for evaluation charts.
function EChartPanel({ option, height }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !window.echarts) return;
    const chart = window.echarts.init(ref.current, _isDark() ? 'dark' : null, { renderer: 'canvas' });
    chartRef.current = chart;
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (chartRef.current && option) chartRef.current.setOption(option, true);
  }, [option]);

  useEffect(() => {
    if (chartRef.current) chartRef.current.resize();
  }, [height]);

  if (!window.echarts) {
    return <div className="muted tiny" style={{ padding: 18 }}>Chart library unavailable.</div>;
  }
  return <div ref={ref} style={{ width: '100%', height: height || 280, minHeight: 180 }} />;
}

function PaginatedTable({ columns, rows, page, pageSize, onPage, onExportCsv, emptyText, onRowClick, selectedKey, rowCls }) {
  const safeRows = rows || [];
  const safeCols = columns || [];
  const limit = Number(pageSize || 50);
  const canPrev = Number(page || 1) > 1;
  const canNext = safeRows.length >= limit;
  return (
    <>
      <div className="panel-body flush" style={{ overflow: 'auto' }}>
        {!safeRows.length ? (
          <div className="muted" style={{ padding: 24, textAlign: 'center', fontSize: 12 }}>
            {emptyText || 'No rows.'}
          </div>
        ) : (
          <table className="tbl eval-table">
            <thead>
              <tr>{safeCols.map(col => <th key={col.key} className={col.cls || ''}>{col.label}</th>)}</tr>
            </thead>
            <tbody>
              {safeRows.map((row, idx) => {
                const key = row.id || row.trade_id || row.date || idx;
                const selected = selectedKey && String(selectedKey) === String(row.date || key);
                const extraCls = rowCls ? rowCls(row) : '';
                return (
                  <tr key={key} className={[selected ? 'selected' : '', extraCls].filter(Boolean).join(' ')} onClick={() => onRowClick && onRowClick(row)}>
                    {safeCols.map(col => (
                      <td key={col.key} className={col.cls || ''}>
                        {col.render ? col.render(row) : (row[col.key] ?? '--')}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <div className="eval-pagination">
        <button className="btn sm" disabled={!canPrev} onClick={() => onPage && onPage(Math.max(1, Number(page || 1) - 1))}>Prev</button>
        <span className="mono tiny">Page {page || 1}</span>
        <button className="btn sm" disabled={!canNext} onClick={() => onPage && onPage(Number(page || 1) + 1)}>Next</button>
        {onExportCsv && <button className="btn sm ghost" onClick={onExportCsv}>Export CSV</button>}
      </div>
    </>
  );
}

function ConfirmModal({ open, title, message, confirmText, requireType, danger, onCancel, onConfirm }) {
  const [typed, setTyped] = useState('');
  useEffect(() => { if (open) setTyped(''); }, [open]);
  if (!open) return null;
  const disabled = requireType ? typed.trim() !== requireType : false;
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(11,15,20,0.45)', zIndex: 999, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
      onClick={onCancel}>
      <div onClick={e => e.stopPropagation()} style={{ background: 'var(--paper)', border: '1px solid var(--line-2)', borderRadius: 8, padding: '20px 22px', minWidth: 320, maxWidth: 460, boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8, color: 'var(--ink)' }}>{title}</div>
        <div style={{ fontSize: 12, lineHeight: 1.55, color: 'var(--ink-2)', marginBottom: 14 }}>{message}</div>
        {requireType && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: 'var(--ink-3)', marginBottom: 6 }}>
              Type <b>{requireType}</b> to confirm
            </div>
            <input className="inp" style={{ width: '100%' }} placeholder={requireType}
              value={typed} onChange={e => setTyped(e.target.value)} autoFocus />
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className={`btn ${danger ? 'danger' : 'primary'}`} disabled={disabled} onClick={onConfirm}>
            {confirmText || 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── BrainStatusBadge ─────────────────────────────────────────────────────
// Coloured pill: CALM | NEUTRAL | VOLATILE | AVOID | UNKNOWN
function BrainStatusBadge({ score }) {
  const s = String(score || 'UNKNOWN').toUpperCase();
  const icons = { CALM: '●', NEUTRAL: '○', VOLATILE: '⚡', AVOID: '✕', UNKNOWN: '?' };
  return <span className={`brain-badge ${s}`}>{icons[s] || '?'} {s}</span>;
}

// ── BrainPanel ────────────────────────────────────────────────────────────
// Light-mode card for eval view + live top banner.
// brainData = response from GET /api/strategy/brain/status
function BrainPanel({ brainData, loading, error, onRefresh }) {
  if (loading) {
    return (
      <div className="brain-panel">
        <div className="brain-unavail">Loading brain status…</div>
      </div>
    );
  }
  if (error) {
    return (
      <div className="brain-panel">
        <div className="brain-unavail" style={{ color: 'var(--neg)' }}>Brain status error: {error}</div>
      </div>
    );
  }
  if (!brainData || !brainData.available) {
    return (
      <div className="brain-panel">
        <div className="brain-panel-head">
          <div className="brain-panel-title">
            <span style={{ opacity: 0.5 }}>⬡</span> Brain
          </div>
        </div>
        <div className="brain-unavail">
          No brain_state.json — engine not started or <code>BRAIN_ENABLED=false</code>.
          Run the nightly feature builder and restart the engine to see morning context here.
        </div>
      </div>
    );
  }

  const d = brainData;
  const score = String(d.day_score || 'UNKNOWN').toUpperCase();
  const conf  = d.day_score_confidence != null ? (Number(d.day_score_confidence) * 100).toFixed(0) + '%' : '—';
  const sizeMult = d.size_multiplier != null ? Number(d.size_multiplier).toFixed(2) + '×' : '—';
  const sizeWarn = d.size_multiplier != null && Number(d.size_multiplier) < 1.0;
  const carryLosses = d.carry_consecutive_losses ?? 0;
  const streakDays  = d.losing_streak_days ?? 0;
  const carryWarn   = carryLosses >= 2 || streakDays >= 1;

  const fmtPct = v => v != null ? (Number(v) >= 0 ? '+' : '') + (Number(v) * 100).toFixed(2) + '%' : '—';
  const fmtF4  = v => v != null ? Number(v).toFixed(4) : '—';

  return (
    <div className="brain-panel">
      <div className="brain-panel-head">
        <div className="brain-panel-title">
          <span>⬡</span> Brain
          {d.trade_date && (
            <span style={{ fontWeight: 400, color: 'var(--ink-4)', fontSize: 10, textTransform: 'none' }}>
              {d.trade_date}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <BrainStatusBadge score={score} />
          {onRefresh && (
            <button className="btn sm ghost" onClick={onRefresh} title="Refresh brain status">↻</button>
          )}
        </div>
      </div>

      <div className="brain-panel-body">
        <div className="brain-kpi">
          <div className="brain-kpi-label">Day Score</div>
          <div className="brain-kpi-value"><BrainStatusBadge score={score} /></div>
          <div className="brain-kpi-sub">{conf} confidence</div>
        </div>
        <div className="brain-kpi">
          <div className="brain-kpi-label">Size Multiplier</div>
          <div className={`brain-kpi-value ${sizeWarn ? 'warn' : 'pos'}`} style={{ color: sizeWarn ? 'var(--warn)' : 'var(--pos)' }}>
            {sizeMult}
          </div>
          <div className="brain-kpi-sub">{d.day_score_reason ? d.day_score_reason.slice(0, 38) : '—'}</div>
        </div>
        <div className="brain-kpi">
          <div className="brain-kpi-label">Carry</div>
          <div className={`brain-kpi-value`} style={{ color: carryWarn ? 'var(--warn)' : 'var(--ink-3)', fontSize: 14 }}>
            {carryLosses} consec
          </div>
          <div className="brain-kpi-sub">{streakDays} losing days streak</div>
        </div>
        <div className="brain-kpi">
          <div className="brain-kpi-label">Prior Day P&L</div>
          <div className={`brain-kpi-value`}
               style={{ color: d.prior_day_pnl_pct == null ? 'var(--ink-3)' : Number(d.prior_day_pnl_pct) >= 0 ? 'var(--pos)' : 'var(--neg)', fontSize: 18 }}>
            {fmtPct(d.prior_day_pnl_pct)}
          </div>
          <div className="brain-kpi-sub">{d.last_trade_date || '—'}</div>
        </div>
      </div>

      {(d.regime_rv20 != null || d.regime_sma20_slope != null || d.regime_dist_sma20 != null || d.regime_60d_return != null) && (
        <div className="brain-regime-row">
          <div className="brain-regime-cell">
            <div className="brain-regime-label">RV20</div>
            <div className="brain-regime-val"
                 style={{ color: d.regime_rv20 != null ? (Number(d.regime_rv20) > 0.018 ? 'var(--neg)' : Number(d.regime_rv20) < 0.010 ? 'var(--pos)' : 'var(--warn)') : 'inherit' }}>
              {fmtF4(d.regime_rv20)}
            </div>
          </div>
          <div className="brain-regime-cell">
            <div className="brain-regime-label">SMA20 Slope</div>
            <div className="brain-regime-val"
                 style={{ color: d.regime_sma20_slope != null ? (Number(d.regime_sma20_slope) >= 0 ? 'var(--pos)' : 'var(--neg)') : 'inherit' }}>
              {fmtPct(d.regime_sma20_slope)}
            </div>
          </div>
          <div className="brain-regime-cell">
            <div className="brain-regime-label">SMA20 Dist</div>
            <div className="brain-regime-val">{fmtPct(d.regime_dist_sma20)}</div>
          </div>
          <div className="brain-regime-cell">
            <div className="brain-regime-label">60d Return</div>
            <div className="brain-regime-val"
                 style={{ color: d.regime_60d_return != null ? (Number(d.regime_60d_return) >= 0 ? 'var(--pos)' : 'var(--neg)') : 'inherit' }}>
              {fmtPct(d.regime_60d_return)}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

Object.assign(window, {
  KpiStrip, LWChart, EChartPanel, PaginatedTable, ConfirmModal,
  BrainStatusBadge, BrainPanel,
});
