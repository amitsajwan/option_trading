// Shared components: PriceChart, KpiStrip, TradeTable, DecisionGrid, etc.
/* global React */
const { useState, useEffect, useRef, useMemo, useCallback } = React;
const TC = window.TradingCore;

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
  height, liveIdx, expanded, onExpandChange,
  fitRef,   // optional: caller passes a ref; we store fitContent fn on it
}) {
  const containerRef = useRef(null);
  const chartRef     = useRef(null);
  const seriesRef    = useRef(null);
  const prevUpToRef  = useRef(-1);
  const prevCandRef  = useRef(null);
  // stable refs so click handler isn't a stale closure
  const tradesRef    = useRef(trades);
  const candlesRef   = useRef(candles);
  useEffect(() => { tradesRef.current  = trades;  }, [trades]);
  useEffect(() => { candlesRef.current = candles; }, [candles]);

  const toLW = (c) => ({ time: Math.floor(c.t / 1000), open: c.o, high: c.h, low: c.l, close: c.c });

  // ── Create chart once ──────────────────────────────────────────────────
  useEffect(() => {
    const LC = window.LightweightCharts;
    if (!containerRef.current || !LC) return;

    const chart = LC.createChart(containerRef.current, {
      layout: {
        background: { color: '#F6F4EF' },
        textColor: '#5A6674',
        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(11,15,20,0.05)' },
        horzLines: { color: 'rgba(11,15,20,0.05)' },
      },
      crosshair: { mode: 1 },   // 1 = Normal
      rightPriceScale: { borderColor: 'rgba(11,15,20,0.12)' },
      timeScale: {
        borderColor: 'rgba(11,15,20,0.12)',
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

    const series = chart.addCandlestickSeries({
      upColor:        '#0A8F5C',
      downColor:      '#C23E2F',
      borderUpColor:  '#0A8F5C',
      borderDownColor:'#C23E2F',
      wickUpColor:    '#0A8F5C',
      wickDownColor:  '#C23E2F',
      priceLineVisible: false,
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

    chartRef.current  = chart;
    seriesRef.current = series;
    if (fitRef) fitRef.current = () => chart.timeScale().fitContent();
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null; };
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // ── Feed candle data ───────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current || !candles.length) return;
    const idx = upToIdx == null ? candles.length - 1 : Math.min(upToIdx, candles.length - 1);
    const sameSession = prevCandRef.current === candles;

    if (sameSession && idx === prevUpToRef.current + 1 && prevUpToRef.current >= 0) {
      // single-bar replay increment → fast path
      seriesRef.current.update(toLW(candles[idx]));
    } else {
      // session change or jump → full reset
      const saved = sameSession && chartRef.current
        ? chartRef.current.timeScale().getVisibleRange() : null;
      seriesRef.current.setData(candles.slice(0, idx + 1).map(toLW));
      if (saved) {
        try { chartRef.current.timeScale().setVisibleRange(saved); } catch (_) {
          chartRef.current.timeScale().fitContent();
        }
      } else {
        chartRef.current.timeScale().fitContent();
      }
    }
    prevUpToRef.current = idx;
    prevCandRef.current = candles;
  }, [candles, upToIdx]);

  // ── Rebuild markers ────────────────────────────────────────────────────
  useEffect(() => {
    if (!seriesRef.current) return;
    const visIdx = upToIdx == null ? Infinity : upToIdx;
    const markers = [];

    // Signal dots — fired only, caller already filters to upToIdx + fired
    (signals || []).forEach(sig => {
      const isSel = selectedSignal && selectedSignal === sig;
      markers.push({
        time:     Math.floor(sig.t / 1000),
        position: 'belowBar',
        color:    sig.traded ? '#0A8F5C' : '#B97405',
        shape:    'circle',
        text:     '',
        size:     isSel ? 2 : 0.7,
      });
    });

    // Trade entry / exit arrows
    (trades || []).forEach(tr => {
      if (tr.entryIdx > visIdx) return;
      const isSel      = selectedTrade && tr.id === selectedTrade.id;
      const entryColor = tr.dir === 'LONG' ? '#0A8F5C' : '#C23E2F';
      const pnlColor   = tr.pnlPct >= 0   ? '#0A8F5C' : '#C23E2F';
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
          text:     isSel ? ((tr.pnlPct >= 0 ? '+' : '') + tr.pnlPct.toFixed(2) + '%') : '',
          size:     isSel ? 2 : 1,
        });
      }
    });

    markers.sort((a, b) => a.time - b.time);
    seriesRef.current.setMarkers(markers);
  }, [trades, signals, selectedTrade, selectedSignal, upToIdx, candles]);

  // ── Navigate when selection changes ───────────────────────────────────
  useEffect(() => {
    if (!chartRef.current) return;
    if (selectedTrade) {
      const entryTs = Math.floor(selectedTrade.t / 1000);
      const exitC   = candles[selectedTrade.exitIdx];
      const exitTs  = exitC ? Math.floor(exitC.t / 1000) : entryTs + 1800;
      const span    = Math.max(exitTs - entryTs, 300);
      const buf     = Math.round(span * 0.5);
      try { chartRef.current.timeScale().setVisibleRange({ from: entryTs - buf, to: exitTs + buf }); } catch (_) {}
    } else if (selectedSignal) {
      const ts = Math.floor(selectedSignal.t / 1000);
      try { chartRef.current.timeScale().setVisibleRange({ from: ts - 600, to: ts + 900 }); } catch (_) {}
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

// Keep alias so any code that still references PriceChart doesn't break.
const PriceChart = LWChart;

// ── TRADE TABLE ──────────────────────────────────────────────────────────
function TradeTable({ trades, selectedId, onSelect, flashId }) {
  if (!trades.length) {
    return (
      <div className="muted" style={{ padding: 24, textAlign: 'center', fontSize: 12 }}>
        No trades in this session yet.
      </div>
    );
  }
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Time</th>
          <th>Strategy</th>
          <th>Dir</th>
          <th className="r mobile-hide">Qty</th>
          <th className="r mobile-hide">Entry</th>
          <th className="r mobile-hide">Exit</th>
          <th className="r">PnL</th>
          <th className="r mobile-hide">Hold</th>
        </tr>
      </thead>
      <tbody>
        {trades.map(tr => (
          <tr key={tr.id}
            className={[
              selectedId === tr.id ? 'selected' : '',
              flashId === tr.id ? (tr.pnlPct >= 0 ? 'flash-pos' : 'flash-neg') : '',
            ].filter(Boolean).join(' ')}
            onClick={() => onSelect && onSelect(tr)}>
            <td className="muted">{tr.tLabel}</td>
            <td>{tr.strat}</td>
            <td><span className={`chip ${tr.dir === 'LONG' ? 'pos' : 'neg'}`}>{tr.dir}</span></td>
            <td className="r mobile-hide">{tr.qty}</td>
            <td className="r mobile-hide">{tr.entry.toFixed(2)}</td>
            <td className="r mobile-hide">{tr.exit.toFixed(2)}</td>
            <td className={`r ${tr.pnlPct >= 0 ? 'pos' : 'neg'}`}>{TC.fmtSigned(tr.pnlPct, 2, '%')}</td>
            <td className="r mobile-hide">{tr.hold}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── DECISION GRID (votes / signals) ──────────────────────────────────────
function DecisionGrid({ signals, selectedId, onSelect }) {
  if (!signals.length) {
    return <div className="muted" style={{ padding: 24, textAlign: 'center', fontSize: 12 }}>No signals yet.</div>;
  }
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Time</th>
          <th>Strategy</th>
          <th>Dir</th>
          <th className="r">Conf</th>
          <th>State</th>
          <th>Outcome</th>
          <th className="mobile-hide">Reason</th>
        </tr>
      </thead>
      <tbody>
        {signals.map((sig, i) => (
          <tr key={i}
            className={selectedId === sig ? 'selected' : ''}
            onClick={() => onSelect && onSelect(sig)}>
            <td className="muted">{TC.fmtTime(new Date(sig.t))}</td>
            <td>{sig.strat}</td>
            <td><span className={`chip ${sig.dir === 'LONG' ? 'pos' : 'neg'}`}>{sig.dir}</span></td>
            <td className="r">{(sig.conf * 100).toFixed(0)}%</td>
            <td>
              {sig.fired
                ? <span className="chip pos"><span className="dot"></span>fired</span>
                : <span className="chip"><span className="dot"></span>held</span>}
            </td>
            <td>
              {sig.fired
                ? (sig.traded
                    ? <span className="chip pos" style={{ fontSize: 10 }}>&#x2192; trade</span>
                    : <span className="chip" style={{ fontSize: 10, opacity: 0.6 }}>&#x2192; skipped</span>)
                : <span className="muted" style={{ fontSize: 10 }}>—</span>}
            </td>
            <td className="mobile-hide muted">{sig.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── DECISION DETAIL (probabilities) ──────────────────────────────────────
function DecisionDetail({ trade, sig, onClose, expanded, setExpanded }) {
  if (!sig) {
    return (
      <div className="muted" style={{ padding: '22px 14px', textAlign: 'center', fontSize: 12 }}>
        Tap a trade or signal to inspect the decision.
      </div>
    );
  }
  const m = sig.metrics || {};
  const probs = [
    { k: 'entry_prob', v: m.entry_prob, cls: 'info' },
    { k: 'trade_prob', v: m.trade_prob, cls: 'info' },
    { k: 'ce_prob', v: m.ce_prob, cls: 'pos' },
    { k: 'pe_prob', v: m.pe_prob, cls: 'neg' },
    { k: 'recipe_prob', v: m.recipe_prob, cls: '' },
    { k: 'recipe_margin', v: m.recipe_margin, cls: '' },
  ];
  const stopBasis = trade?.stopBasis || null;
  const riskRows = trade ? [
    { k: 'risk_basis', v: stopBasis },
    { k: stopBasis === 'underlying' ? 'underlying_stop_pct' : 'stop_loss_pct', v: trade.stopLossPct },
    { k: stopBasis === 'underlying' ? 'underlying_target_pct' : 'target_pct', v: trade.targetPct },
    { k: 'max_hold_bars', v: trade.maxHoldBars },
    { k: stopBasis === 'underlying' ? 'entry_futures_price' : 'stop_price', v: stopBasis === 'underlying' ? trade.entryFuturesPrice : trade.stopPrice },
    { k: stopBasis === 'underlying' ? 'underlying_stop_level' : 'stop_price', v: stopBasis === 'underlying' ? trade.underlyingStopPrice : trade.stopPrice },
    { k: 'stop_trigger_candle', v: trade.stopTriggerCandle },
  ].filter(x => x.v != null && x.v !== '') : [];
  const riskLabels = {
    risk_basis: 'risk_basis',
    stop_loss_pct: 'stop_loss_pct',
    underlying_stop_pct: 'stop_loss_pct',
    target_pct: 'target_pct',
    underlying_target_pct: 'target_pct',
    max_hold_bars: 'max_hold_bars',
    stop_price: 'stop_price',
    entry_futures_price: 'entry_futures_price',
    underlying_stop_level: 'stop_level',
    stop_trigger_candle: 'trigger_candle',
  };
  function fmtRiskValue(row) {
    if (typeof row.v === 'number' && row.k.indexOf('_pct') >= 0) return (row.v * 100).toFixed(2) + '%';
    if (typeof row.v === 'number' && (row.k.indexOf('price') >= 0 || row.k.indexOf('level') >= 0)) return row.v.toFixed(2);
    return String(row.v);
  }
  return (
    <>
      <div className="decision-head">
        <span className={`decision-dirbadge ${sig.dir}`}>{sig.dir}</span>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--ink)' }}>
            {sig.strat}
            <span className="mono tiny muted" style={{ marginLeft: 8 }}>{TC.fmtTime(new Date(sig.t))}</span>
          </div>
          <div className="mono tiny" style={{ color: 'var(--ink-3)', marginTop: 2 }}>
            {sig.fired ? 'FIRED' : 'HELD'} · {sig.reason} · regime {sig.regime}
          </div>
          {sig.fired && (
            <div className="mono tiny" style={{ marginTop: 3, color: sig.traded ? 'var(--pos)' : 'var(--ink-3)' }}>
              {sig.traded
                ? 'Execution: position opened \u2192 trade recorded'
                : 'Execution: signal skipped \u2014 no position opened by runtime'}
            </div>
          )}
        </div>
        <button className="btn sm ghost" onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      </div>
      <div className="panel-body">
        {trade && (
          <>
            <div className="rowx" style={{ marginBottom: 8 }}>
              <span className="muted tiny">Entry logic</span>
              <span className="mono tiny" style={{ color: 'var(--ink)' }}>{trade.entryReason || sig.reason}</span>
            </div>
            {(trade.entryDetail || sig.detail) && (
              <div className="mono tiny" style={{ color: 'var(--ink-3)', marginBottom: 10, lineHeight: 1.5 }}>
                {trade.entryDetail || sig.detail}
              </div>
            )}
            <div className="rowx" style={{ marginBottom: 8 }}>
              <span className="muted tiny">Exit logic</span>
              <span className="mono tiny" style={{ color: trade.exitReason === 'TARGET_HIT' ? 'var(--pos)' : 'var(--neg)' }}>
                {trade.exitReason || '--'}
              </span>
            </div>
            {trade.exitDetail && (
              <div className="mono tiny" style={{ color: 'var(--ink-3)', marginBottom: 10, lineHeight: 1.5 }}>
                {trade.exitDetail}
              </div>
            )}
            {trade.stopTriggerDetail && (
              <div className="mono tiny" style={{ color: 'var(--ink-3)', marginBottom: 10, lineHeight: 1.5 }}>
                {trade.stopTriggerDetail}
              </div>
            )}
            {!!riskRows.length && (
              <div style={{ marginBottom: 12 }}>
                {riskRows.map(r => (
                  <div className="rowx" key={r.k} style={{ marginBottom: 4 }}>
                    <span className="muted tiny">{riskLabels[r.k] || r.k}</span>
                    <span className="mono tiny" style={{ color: 'var(--ink)' }}>
                      {fmtRiskValue(r)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        <div className="rowx" style={{ marginBottom: 10 }}>
          <span className="muted tiny">Confidence</span>
          <span className="mono" style={{ fontSize: 15, color: 'var(--ink)' }}>{(sig.conf * 100).toFixed(1)}%</span>
        </div>
        <div className="prob-bar" style={{ marginBottom: 14 }}>
          <span style={{ width: (sig.conf * 100) + '%' }} />
        </div>
        {!trade && sig.detail && (
          <div className="mono tiny" style={{ color: 'var(--ink-3)', marginBottom: 14, lineHeight: 1.5 }}>
            {sig.detail}
          </div>
        )}
        {expanded && probs.map(p => (
          <div className="prob-row" key={p.k}>
            <span className="k">{p.k}</span>
            <div className={`prob-bar ${p.cls}`}>
              <span style={{ width: (Math.max(0, Math.min(1, p.v)) * 100) + '%' }} />
            </div>
            <span className="v">{(p.v * 100).toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </>
  );
}

// ── ALERTS ────────────────────────────────────────────────────────────────
function AlertList({ alerts }) {
  return (
    <div>
      {alerts.map((a, i) => (
        <div className={`alert ${a.level}`} key={i}>
          <div className="bar"></div>
          <div className="msg" dangerouslySetInnerHTML={{ __html: a.msg }} />
          <div className="t">{a.t}</div>
        </div>
      ))}
    </div>
  );
}

// ── STRATEGY CONTRIBUTION BARS ───────────────────────────────────────────
function StrategyBars({ rows }) {
  if (!rows.length) return <div className="muted tiny">No contribution data yet.</div>;
  const max = Math.max.apply(null, rows.map(r => Math.abs(r.value)).concat([0.01]));
  return (
    <div>
      {rows.map((r, i) => {
        const w = (Math.abs(r.value) / max) * 100;
        const pos = r.value >= 0;
        return (
          <div className="sbar-row" key={i}>
            <span className="sbar-label">{r.label}</span>
            <div className="sbar-track">
              <div className="sbar-fill"
                style={{
                  width: w + '%',
                  left: pos ? '0' : 'auto',
                  right: pos ? 'auto' : '0',
                  background: pos ? 'var(--pos)' : 'var(--neg)',
                  opacity: 0.85,
                }} />
            </div>
            <span className={`sbar-val ${pos ? 'pos' : 'neg'}`}
              style={{ color: pos ? 'var(--pos)' : 'var(--neg)' }}>
              {TC.fmtSigned(r.value, 2, '%')}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── CONFIRM MODAL ─────────────────────────────────────────────────────────
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

Object.assign(window, {
  KpiStrip, PriceChart, TradeTable, DecisionGrid, DecisionDetail,
  AlertList, StrategyBars, ConfirmModal,
});
