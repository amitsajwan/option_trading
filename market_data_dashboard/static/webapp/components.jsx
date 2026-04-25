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

// ── PRICE CHART ──────────────────────────────────────────────────────────
// SVG candlestick with trade markers and crosshair tooltip.
function PriceChart({ candles, upToIdx, trades, selectedTradeId, onSelectTrade, height, liveIdx }) {
  const [hover, setHover] = useState(null);
  const wrapRef = useRef(null);
  const [width, setWidth] = useState(800);
  const H = height || 340;
  const PAD = { t: 16, r: 48, b: 28, l: 10 };

  useEffect(() => {
    function measure() {
      if (wrapRef.current) setWidth(wrapRef.current.clientWidth);
    }
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  const visibleCount = (upToIdx == null ? candles.length : Math.min(upToIdx + 1, candles.length));
  const visible = candles.slice(0, visibleCount);
  const { min, max } = useMemo(() => {
    if (!visible.length) return { min: 0, max: 1 };
    let mn = Infinity, mx = -Infinity;
    visible.forEach(c => { if (c.l < mn) mn = c.l; if (c.h > mx) mx = c.h; });
    const pad = (mx - mn) * 0.05;
    return { min: mn - pad, max: mx + pad };
  }, [visible, visibleCount]);

  // Always render full-session x domain so live feels like it's filling up.
  const W = width;
  const xInner = W - PAD.l - PAD.r;
  const yInner = H - PAD.t - PAD.b;
  const step = xInner / Math.max(1, candles.length);
  const xOf = (i) => PAD.l + i * step + step / 2;
  const yOf = (p) => PAD.t + yInner - ((p - min) / (max - min)) * yInner;
  const cw = Math.max(1.2, step * 0.7);

  function handleMove(e) {
    const r = wrapRef.current.getBoundingClientRect();
    const x = e.clientX - r.left;
    const rawIdx = Math.round((x - PAD.l) / step);
    const idx = Math.max(0, Math.min(visible.length - 1, rawIdx));
    if (!visible[idx]) return;
    setHover({ idx: idx, x: xOf(idx), candle: visible[idx] });
  }
  function handleLeave() { setHover(null); }

  // Trade markers — y-position uses the candle's close price (futures chart scale),
  // not the option premium which is on a completely different scale.
  const markers = useMemo(() => {
    const mk = [];
    (trades || []).forEach(tr => {
      if (tr.entryIdx != null && tr.entryIdx < visibleCount) {
        const candlePrice = visible[tr.entryIdx]?.c ?? tr.entry;
        mk.push({ id: tr.id + '-e', role: 'entry', dir: tr.dir, idx: tr.entryIdx, price: candlePrice, trade: tr });
      }
      if (tr.exitIdx != null && tr.exitIdx < visibleCount) {
        const candlePrice = visible[tr.exitIdx]?.c ?? tr.exit;
        mk.push({ id: tr.id + '-x', role: 'exit', dir: tr.dir, idx: tr.exitIdx, price: candlePrice, trade: tr });
      }
    });
    return mk;
  }, [trades, visibleCount, visible]);

  // Y-axis gridlines
  const yTicks = useMemo(() => {
    const arr = [];
    const n = 4;
    for (let i = 0; i <= n; i++) arr.push(min + (max - min) * (i / n));
    return arr;
  }, [min, max]);

  // Find last tick time for label
  const lastCandle = visible[visible.length - 1];

  return (
    <div ref={wrapRef} className="chart-wrap" style={{ height: H, userSelect: 'none' }}>
      <svg width={W} height={H} onMouseMove={handleMove} onMouseLeave={handleLeave}>
        {/* Y gridlines */}
        {yTicks.map((p, i) => (
          <g key={`y${i}`}>
            <line x1={PAD.l} x2={W - PAD.r} y1={yOf(p)} y2={yOf(p)}
              stroke="rgba(11,15,20,0.05)" strokeDasharray="2 4" />
            <text x={W - PAD.r + 6} y={yOf(p) + 3} fontSize="10"
              fontFamily="var(--f-mono)" fill="var(--ink-3)">{p.toFixed(0)}</text>
          </g>
        ))}
        {/* Candles */}
        {visible.map((c, i) => {
          const up = c.c >= c.o;
          const color = up ? 'var(--pos)' : 'var(--neg)';
          return (
            <g key={i}>
              <line x1={xOf(i)} x2={xOf(i)} y1={yOf(c.h)} y2={yOf(c.l)} stroke={color} strokeWidth="1" />
              <rect
                x={xOf(i) - cw/2} y={yOf(Math.max(c.o, c.c))}
                width={cw} height={Math.max(1, Math.abs(yOf(c.o) - yOf(c.c)))}
                fill={up ? color : color} opacity={up ? 0.85 : 1}
              />
            </g>
          );
        })}
        {/* Live marker */}
        {liveIdx != null && visible[liveIdx] && (
          <g>
            <circle cx={xOf(liveIdx)} cy={yOf(visible[liveIdx].c)} r="4" fill="var(--pos)" opacity="0.9" />
            <circle cx={xOf(liveIdx)} cy={yOf(visible[liveIdx].c)} r="9" fill="none" stroke="var(--pos)" strokeOpacity="0.3">
              <animate attributeName="r" from="4" to="14" dur="1.5s" repeatCount="indefinite" />
              <animate attributeName="stroke-opacity" from="0.4" to="0" dur="1.5s" repeatCount="indefinite" />
            </circle>
          </g>
        )}
        {/* Trade markers */}
        {markers.map(m => {
          const y = yOf(m.price);
          const x = xOf(m.idx);
          const isEntry = m.role === 'entry';
          const isLong = m.dir === 'LONG';
          const color = isLong ? 'var(--pos)' : 'var(--neg)';
          const selected = selectedTradeId && m.trade.id === selectedTradeId;
          if (isEntry) {
            const dy = isLong ? 10 : -10;
            return (
              <g key={m.id} style={{ cursor: 'pointer' }} onClick={() => onSelectTrade && onSelectTrade(m.trade)}>
                <polygon points={`${x},${y + dy * 0.2} ${x-5},${y + dy} ${x+5},${y + dy}`}
                  fill={color} stroke={selected ? 'var(--ink)' : 'none'} strokeWidth="1.5" />
              </g>
            );
          }
          return (
            <g key={m.id} style={{ cursor: 'pointer' }} onClick={() => onSelectTrade && onSelectTrade(m.trade)}>
              <circle cx={x} cy={y} r="4" fill="var(--paper)" stroke={color} strokeWidth="2" />
              {selected && <circle cx={x} cy={y} r="7" fill="none" stroke="var(--ink)" />}
            </g>
          );
        })}
      </svg>
      {/* Crosshair */}
      {hover && (
        <>
          <div className="chart-crosshair-v"
            style={{ left: hover.x, opacity: 1 }} />
          <div className={`chart-tip show`}
            style={{ left: Math.min(hover.x + 14, W - 170), top: 16 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{hover.candle.label}</div>
            <div className="chart-tip-row"><span className="k">O</span><span>{hover.candle.o.toFixed(2)}</span></div>
            <div className="chart-tip-row"><span className="k">H</span><span>{hover.candle.h.toFixed(2)}</span></div>
            <div className="chart-tip-row"><span className="k">L</span><span>{hover.candle.l.toFixed(2)}</span></div>
            <div className="chart-tip-row"><span className="k">C</span><span>{hover.candle.c.toFixed(2)}</span></div>
            <div className="chart-tip-row"><span className="k">Vol</span><span>{TC.fmtCompact(hover.candle.v)}</span></div>
          </div>
        </>
      )}
    </div>
  );
}

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
  const riskRows = trade ? [
    { k: 'stop_loss_pct', v: trade.stopLossPct },
    { k: 'target_pct', v: trade.targetPct },
    { k: 'max_hold_bars', v: trade.maxHoldBars },
    { k: 'stop_price', v: trade.stopPrice },
  ].filter(x => x.v != null && x.v !== '') : [];
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
            {!!riskRows.length && (
              <div style={{ marginBottom: 12 }}>
                {riskRows.map(r => (
                  <div className="rowx" key={r.k} style={{ marginBottom: 4 }}>
                    <span className="muted tiny">{r.k}</span>
                    <span className="mono tiny" style={{ color: 'var(--ink)' }}>
                      {typeof r.v === 'number' && r.k.indexOf('_pct') >= 0 ? (r.v * 100).toFixed(2) + '%' : String(r.v)}
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
