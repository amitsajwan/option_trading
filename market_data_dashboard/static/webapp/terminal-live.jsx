// terminal-live.jsx — Dark Bloomberg terminal for live + replay modes  v16
/* global React, TradingCore, LWChart */
const { useState: _s, useEffect: _e, useMemo: _m, useRef: _r, useCallback: _cb } = React;
const TC = window.TradingCore;

// ── Data helpers ─────────────────────────────────────────────────────────
function _barLabel(session, idx) {
  const c = session?.candles?.[idx];
  if (!c) return '—';
  if (c.label) return c.label;
  return new Date(c.t).toLocaleTimeString('en-IN',
    { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });
}

// Normalize trade fields — real session may use .entry/.exit instead of .entryPx/.exitPx
function _bridgeTrade(tr) {
  const ep = tr.entryPx ?? tr.entry ?? 0;
  const xp = tr.exitPx  ?? tr.exit  ?? 0;
  const rng = Math.abs(ep) * 0.002;
  const rawDir = tr.dir || tr.direction || 'LONG';
  const optionType = tr.optionType ?? tr.option_type ?? (rawDir === 'PE' || rawDir === 'CE' ? rawDir : null);
  const positionSide = String(tr.positionSide || tr.position_side || '').trim().toUpperCase();
  // Chart bias (LONG/SHORT triangles) follows delta; tape "Dir" shows leg + buy/sell.
  const dirMap = { PE: 'SHORT', CE: 'LONG' };
  const chartBias = dirMap[rawDir] || dirMap[rawDir.toUpperCase()] || rawDir;
  const legDir = optionType || rawDir;
  const isLongPremium = positionSide === 'LONG' || (positionSide !== 'SHORT' && chartBias === 'LONG');
  return {
    ...tr, dir: chartBias, legDir, positionSide,
    entryPx: ep, exitPx: xp,
    strat: tr.strat || tr.strategy_name || '—',
    stopPx:   tr.stopPx   ?? (isLongPremium ? ep - rng : ep + rng),
    targetPx: tr.targetPx ?? (isLongPremium ? ep + rng * 2 : ep - rng * 2),
    heldBars: tr.heldBars ?? ((tr.exitIdx ?? 0) - (tr.entryIdx ?? 0)),
    conf:     tr.conf ?? tr.confidence ?? 0.65,
    regime:   tr.regime ?? '—',
    exitReason:  tr.exitReason ?? 'CLOSED',
    entryDetail: tr.entryDetail ?? `Entry at ${ep.toFixed(2)}`,
    exitDetail:  tr.exitDetail  ?? `Exit at ${xp.toFixed(2)} · ${tr.exitReason ?? 'closed'}`,
    strike: tr.strike ?? null,
    optionType,
  };
}

function _makeStrategies(trades) {
  const map = {};
  trades.forEach(tr => {
    const k = tr.strat || '—';
    if (!map[k]) map[k] = { id: k, name: k, pnl: 0, trades: 0, wins: 0 };
    map[k].pnl   += tr.pnlPct || 0;
    map[k].trades++;
    if ((tr.pnlPct || 0) > 0) map[k].wins++;
  });
  return Object.values(map).map(s => ({
    ...s,
    wr:     s.trades ? Math.round(s.wins / s.trades * 100) : 0,
    weight: 1 / Math.max(1, Object.keys(map).length),
    status: s.pnl < -0.1 ? 'penalty' : 'armed',
  })).sort((a, b) => b.pnl - a.pnl);
}

function _makeQuote(session, upToIdx) {
  if (!session?.candles?.length) return null;
  const cands = session.candles;
  const end   = Math.min(upToIdx, cands.length - 1);
  const cur   = cands[end];
  const first = cands[0];
  return {
    symbol:    session.instrument || 'BANKNIFTY FUT',
    spot:      cur.c,
    spotChg:   cur.c - first.o,
    spotChgPct: ((cur.c - first.o) / first.o) * 100,
    dayHigh:   Math.max(...cands.slice(0, end + 1).map(c => c.h)),
    dayLow:    Math.min(...cands.slice(0, end + 1).map(c => c.l)),
  };
}

// ── TickerBar ────────────────────────────────────────────────────────────
function TickerBar({ quote, instrument }) {
  const [clock, setClock] = _s(TC.fmtClock(new Date()));
  _e(() => { const id = setInterval(() => setClock(TC.fmtClock(new Date())), 1000); return () => clearInterval(id); }, []);
  if (!quote) return <div className="ticker-bar"><div className="t-brand"><span className="t-brand-mark"/><b>QUANT</b><span>OPS</span></div><div className="ticker-clock"><span className="t-live-pulse"/><span>{clock}</span></div></div>;
  const cls = quote.spotChg >= 0 ? 'pos' : 'neg';
  return (
    <div className="ticker-bar">
      <div className="t-brand">
        <span className="t-brand-mark"/>
        <b>QUANT</b><span>OPS</span>
        <span className="ver">live</span>
      </div>
      <div className="ticker-pairs">
        <div className="t-spot-big">
          <span className="lbl" style={{fontSize:'9.5px',color:'var(--fg-3)',letterSpacing:'0.10em',textTransform:'uppercase'}}>{quote.symbol}</span>
          <span className="val">{quote.spot.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
          <span className={`delta ${cls}`}>{TC.fmtSigned(quote.spotChg,2)} ({TC.fmtSigned(quote.spotChgPct,2,'%')})</span>
        </div>
        <div className="pair"><span className="lbl">H</span><span className="val">{quote.dayHigh.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
        <div className="pair"><span className="lbl">L</span><span className="val">{quote.dayLow.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
      </div>
      <div className="ticker-clock">
        <span className="t-live-pulse"/>
        <span>{clock}</span>
      </div>
    </div>
  );
}

// ── StatusBar ────────────────────────────────────────────────────────────
function StatusBar({ sessionPnl, tradesCount, winRate, regime, engine, ws, onModeSwitch, onHaltClick }) {
  const pnlCls = sessionPnl >= 0 ? 'pos' : 'neg';
  return (
    <div className="t-status-bar">
      <div className="t-mode-toggle">
        <button className="active"><span className="dot live"/>Live</button>
        <button onClick={() => onModeSwitch('replay')}><span className="dot replay"/>Replay</button>
        <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
      </div>
      <div className="t-status-cells">
        <div className="t-scell big"><span className="k">Session P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl,2)}</span></div>
        <div className="t-scell"><span className="k">Trades</span><span className="v">{tradesCount}<span className="sub">/ {winRate}% WR</span></span></div>
        <div className="t-scell"><span className="k">Regime</span><span className="v" style={{color:'var(--info)',fontSize:'11px'}}>{regime || '—'}</span></div>
        <div className="t-scell"><span className="k">Engine</span><span className="v" style={{fontSize:'11px'}}>{engine || '—'}</span></div>
        <div className="t-scell"><span className="k">WS</span><span className={`v ${ws === 'connected' ? 'pos' : 'warn'}`} style={{fontSize:'11px'}}>{ws}</span></div>
      </div>
      <div className="t-status-actions">
        <button className="t-btn danger" onClick={onHaltClick}>⏻ Halt <span className="kbd">⌘.</span></button>
      </div>
    </div>
  );
}

// ── Brain Status (compact dark-theme widget) ──────────────────────────────
function BrainStatusCompact({ brainData }) {
  if (!brainData || !brainData.available) return null;
  const d = brainData;
  const score = String(d.day_score || 'UNKNOWN').toUpperCase();
  const scoreColor = {
    CALM:     'var(--pos)',
    NEUTRAL:  'var(--fg-3)',
    VOLATILE: 'var(--warn)',
    AVOID:    'var(--neg)',
    UNKNOWN:  'var(--fg-4)',
  }[score] || 'var(--fg-4)';
  const fmtF = v => v != null ? Number(v).toFixed(4) : '—';
  const fmtPct = v => v != null ? (Number(v)>=0?'+':'') + (Number(v)*100).toFixed(2)+'%' : '—';
  const sizeWarn = d.size_multiplier != null && Number(d.size_multiplier) < 1.0;
  const carryWarn = (d.carry_consecutive_losses || 0) >= 2 || (d.losing_streak_days || 0) >= 1;
  return (
    <div style={{flexShrink:0}}>
      <div className="t-section-head">Brain</div>
      <div style={{padding:'6px 12px',display:'grid',gap:4}}>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>day_score</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,fontWeight:700,color:scoreColor}}>{score}</span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>conf</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:'var(--fg-2)'}}>
            {d.day_score_confidence != null ? (Number(d.day_score_confidence)*100).toFixed(0)+'%' : '—'}
          </span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>size_mult</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:sizeWarn?'var(--warn)':'var(--pos)'}}>
            {d.size_multiplier != null ? Number(d.size_multiplier).toFixed(2)+'×' : '—'}
          </span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>carry</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:carryWarn?'var(--warn)':'var(--fg-3)'}}>
            {d.carry_consecutive_losses || 0} L · {d.losing_streak_days || 0}d
          </span>
        </div>
        {d.regime_rv20 != null && (
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>rv20</span>
            <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:Number(d.regime_rv20)>0.018?'var(--neg)':Number(d.regime_rv20)<0.010?'var(--pos)':'var(--warn)'}}>
              {fmtF(d.regime_rv20)}
            </span>
          </div>
        )}
        {d.regime_sma20_slope != null && (
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>sma_slope</span>
            <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:Number(d.regime_sma20_slope)>=0?'var(--pos)':'var(--neg)'}}>
              {fmtPct(d.regime_sma20_slope)}
            </span>
          </div>
        )}
        {d.day_score_reason && (
          <div style={{marginTop:2,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-4)',
                       wordBreak:'break-all',lineHeight:1.4}}>
            {d.day_score_reason.slice(0, 60)}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Engine Roster (left rail) ────────────────────────────────────────────
function EngineRoster({ strategies, dailyRisk, brainData }) {
  const risk = dailyRisk || 0;
  return (
    <div className="t-rail">
      <BrainStatusCompact brainData={brainData} />
      <div className="t-section-head" style={{borderTop:0}}>Strategy Roster</div>
      <div style={{overflowY:'auto',flexShrink:0}}>
        {strategies.length === 0
          ? <div style={{padding:'10px 12px',color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:10}}>No trades yet</div>
          : strategies.map(s => (
            <div key={s.id} className="t-strat-row">
              <div>
                <div className="t-strat-name">{s.name}</div>
                <div className="t-strat-meta">
                  <span>{s.trades}t</span><span>·</span>
                  <span>{s.wr}%wr</span>
                </div>
                <div className={`t-strat-bar ${s.pnl < 0 ? 'neg' : ''}`}>
                  <span style={{width: Math.min(100, Math.abs(s.pnl) / 0.5 * 100) + '%'}}/>
                </div>
              </div>
              <div style={{textAlign:'right'}}>
                <div className={`t-strat-pnl ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{TC.fmtPct(s.pnl,2)}</div>
                <div className={`t-strat-status ${s.status}`}>{s.status}</div>
              </div>
            </div>
          ))
        }
      </div>
      <div className="t-section-head">Risk</div>
      <div style={{flexShrink:0}}>
        <div className="t-gauge">
          <span className="lbl">Daily risk used</span>
          <span className="val">{(risk*100).toFixed(0)}%</span>
          <div className="track"><span style={{width:Math.min(100,risk*100)+'%'}}/></div>
        </div>
      </div>
      <div style={{flex:1}}/>
      <div style={{padding:'6px 12px',borderTop:'1px solid var(--line-2)',background:'var(--bg-0)',fontFamily:'var(--f-mono)',fontSize:'9.5px',color:'var(--fg-3)'}}>
        <div style={{display:'flex',justifyContent:'space-between'}}>
          <span>OPS · amitsajwan</span>
          <span style={{color:'var(--pos)'}}>● ACTIVE</span>
        </div>
      </div>
    </div>
  );
}

// ── SVG Candlestick Chart ────────────────────────────────────────────────
function TermChart({ session, candles, trades, selectedTrade, onSelectTrade, upToIdx, flashIdx }) {
  const W = 1000, H = 360;
  const PAD = { l: 8, r: 60, t: 8, b: 22 };
  const iW = W - PAD.l - PAD.r, iH = H - PAD.t - PAD.b;
  const visible = candles.slice(0, upToIdx + 1);
  const slot = iW / Math.max(1, visible.length);
  const bodyW = Math.max(2, slot - 1.5);

  const [pMin, pMax] = _m(() => {
    let lo = Infinity, hi = -Infinity;
    visible.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
    const pad = (hi - lo) * 0.10;
    return [lo - pad, hi + pad];
  }, [visible]);

  const xOf = i => PAD.l + i * slot + slot / 2;
  const yOf = p => PAD.t + (pMax - p) / (pMax - pMin) * iH;

  const vwapPath = _m(() => {
    let cv = 0, cc = 0;
    return visible.map((c, i) => {
      const tp = (c.h + c.l + c.c) / 3;
      cv += tp * (c.v || 1); cc += (c.v || 1);
      return `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(cv/cc).toFixed(1)}`;
    }).join(' ');
  }, [visible, pMin, pMax]);

  const ticks = _m(() => {
    const out = [];
    const step = Math.ceil((pMax - pMin) / 6 / 10) * 10 || 10;
    let p = Math.ceil(pMin / step) * step;
    while (p < pMax) { out.push(p); p += step; }
    return out;
  }, [pMin, pMax]);

  const timeTicks = _m(() => {
    const out = [];
    const n = visible.length;
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) out.push(i);
    if (n > 0 && out[out.length - 1] !== n - 1) out.push(n - 1);
    return out;
  }, [visible.length]);

  const [hover, setHover] = _s(null);
  const svgRef = _r(null);

  const handleMove = _cb(e => {
    const r = svgRef.current?.getBoundingClientRect();
    if (!r) return;
    const x = (e.clientX - r.left) * (W / r.width);
    const i = Math.floor((x - PAD.l) / slot);
    setHover(i >= 0 && i < visible.length ? i : null);
  }, [visible.length, slot]);

  const curPx = visible[visible.length - 1]?.c || 0;
  const curY  = yOf(curPx);

  return (
    <div className="t-chart-area">
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           onMouseMove={handleMove} onMouseLeave={() => setHover(null)}>
        <g className="t-chart-grid">
          {ticks.map(p => <line key={p} x1={PAD.l} x2={W-PAD.r} y1={yOf(p)} y2={yOf(p)}/>)}
        </g>

        {selectedTrade && (() => {
          const t = selectedTrade;
          const eiN = Math.min(t.entryIdx, visible.length - 1);
          const exN = Math.min(t.exitIdx,  visible.length - 1);
          if (eiN < 0) return null;
          return (
            <g>
              <rect x={xOf(eiN)-bodyW/2} y={Math.min(yOf(t.stopPx),yOf(t.targetPx))}
                width={xOf(exN)-xOf(eiN)+bodyW}
                height={Math.abs(yOf(t.stopPx)-yOf(t.targetPx))} className="t-chart-trade-band"/>
              <line x1={PAD.l} x2={W-PAD.r} y1={yOf(t.stopPx)}   y2={yOf(t.stopPx)}   className="t-chart-stop-line"/>
              <line x1={PAD.l} x2={W-PAD.r} y1={yOf(t.targetPx)} y2={yOf(t.targetPx)} className="t-chart-target-line"/>
              <text x={W-PAD.r+3} y={yOf(t.stopPx)+3} fill="var(--neg)" fontFamily="var(--f-mono)" fontSize="9">STP {t.stopPx.toFixed(0)}</text>
              <text x={W-PAD.r+3} y={yOf(t.targetPx)+3} fill="var(--pos)" fontFamily="var(--f-mono)" fontSize="9">TGT {t.targetPx.toFixed(0)}</text>
            </g>
          );
        })()}

        {visible.map((c, i) => {
          const up = c.c >= c.o;
          const x = xOf(i);
          const bTop = yOf(Math.max(c.o, c.c));
          const bBot = yOf(Math.min(c.o, c.c));
          const flash = i === flashIdx ? ' flash' : '';
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={yOf(c.h)} y2={yOf(c.l)} className={`t-candle-wick ${up?'up':'down'}`} strokeWidth="1"/>
              <rect x={x-bodyW/2} y={bTop} width={bodyW} height={Math.max(1,bBot-bTop)}
                    className={`t-candle-body ${up?'up':'down'}${flash}`} strokeWidth="0.5"/>
            </g>
          );
        })}

        <path d={vwapPath} className="t-chart-vwap"/>

        {trades.filter(t => t.entryIdx < visible.length).map(t => {
          const isSel = selectedTrade?.id === t.id;
          const cx = xOf(Math.min(t.entryIdx, visible.length - 1));
          const cy = yOf(t.entryPx);
          const color = t.dir === 'LONG' ? 'var(--pos)' : 'var(--neg)';
          const d = t.dir === 'LONG'
            ? `M ${cx} ${cy+13} L ${cx-4} ${cy+21} L ${cx+4} ${cy+21} Z`
            : `M ${cx} ${cy-13} L ${cx-4} ${cy-21} L ${cx+4} ${cy-21} Z`;
          return (
            <path key={t.id} d={d} fill={color} fillOpacity={isSel?1:0.55}
                  stroke={color} strokeWidth="1" className="t-trade-marker"
                  onClick={() => onSelectTrade(t)}/>
          );
        })}

        <g>
          <line x1={PAD.l} x2={W-PAD.r} y1={curY} y2={curY} stroke="var(--accent)" strokeWidth="1" strokeDasharray="1 2" opacity="0.6"/>
          <rect x={W-PAD.r+2} y={curY-9} width={54} height={18} fill="var(--accent)" rx="2"/>
          <text x={W-PAD.r+5} y={curY+4} fill="#1a1100" fontFamily="var(--f-mono)" fontSize="10" fontWeight="700">{curPx.toFixed(0)}</text>
        </g>

        <g>
          {ticks.map(p => <text key={p} x={W-PAD.r+3} y={yOf(p)+3} fontFamily="var(--f-mono)" fontSize="9" fill="var(--fg-3)">{p.toFixed(0)}</text>)}
        </g>
        <g>
          {timeTicks.map(i => (
            <text key={i} x={xOf(i)} y={H-6} fontFamily="var(--f-mono)" fontSize="9" fill="var(--fg-3)" textAnchor="middle">
              {_barLabel(session, i)}
            </text>
          ))}
        </g>

        {hover != null && (() => {
          const c = visible[hover];
          if (!c) return null;
          const x = xOf(hover);
          const tipX = hover > visible.length * 0.7 ? x - 170 : x + 8;
          return (
            <g>
              <line x1={x} x2={x} y1={PAD.t} y2={H-PAD.b} className="t-chart-crosshair"/>
              <rect x={tipX} y={PAD.t+4} width={164} height={80} fill="var(--bg-2)" stroke="var(--line-3)" rx="2"/>
              <text x={tipX+8} y={PAD.t+18} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-2)">{_barLabel(session,hover)}</text>
              <text x={tipX+8} y={PAD.t+34} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">O <tspan fill="var(--fg-1)" fontWeight="600">{c.o.toFixed(0)}</tspan></text>
              <text x={tipX+70} y={PAD.t+34} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">H <tspan fill="var(--pos)" fontWeight="600">{c.h.toFixed(0)}</tspan></text>
              <text x={tipX+8} y={PAD.t+50} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">L <tspan fill="var(--neg)" fontWeight="600">{c.l.toFixed(0)}</tspan></text>
              <text x={tipX+70} y={PAD.t+50} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">C <tspan fill={c.c>=c.o?'var(--pos)':'var(--neg)'} fontWeight="600">{c.c.toFixed(0)}</tspan></text>
            </g>
          );
        })()}
      </svg>
    </div>
  );
}

// ── DiagPanel: current model + available models + blocker funnel ─────────
// Operator answer to "why no trades on this date?" — reads runtime_config +
// available_models from /api/strategy/current/state and per-date blocker
// counts from /api/strategy/blocker-funnel.
function _fmtProb(p) {
  if (p == null || isNaN(p)) return '—';
  return (p * 100).toFixed(1) + '%';
}
function _stageDiag(row, stage) {
  const md = row?.model_diagnostics || {};
  return md?.[stage] || {};
}
function _shortHash(h) {
  const s = String(h || '').trim();
  return s ? s.slice(0, 8) : '—';
}
function _outcomeColor(oc) {
  if (oc === 'entry_taken' || oc === 'exit_taken') return 'var(--pos)';
  if (oc === 'blocked') return 'var(--neg)';
  if (oc === 'hold') return 'var(--warn)';
  return 'var(--fg-3)';
}
function DiagPanel({ diag }) {
  const [tlFilter, setTlFilter] = _s('blocked');
  const [tlCollapse, setTlCollapse] = _s(true);
  if (!diag) {
    return <div style={{padding:'12px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',fontSize:11}}>Diagnostics unavailable in this mode.</div>;
  }
  const { runtimeConfig, availableModels, blockerFunnel, timeline, brainData, loading, error, date, onRefresh, onTimelineFilterChange, onTimelineCollapseChange, activeRunId } = diag;
  const rc = runtimeConfig || {};
  const models = Array.isArray(availableModels) ? availableModels : [];
  const bf = blockerFunnel || {};
  const gates = Array.isArray(bf.primary_blocker_gates) ? bf.primary_blocker_gates : [];
  const reasons = Array.isArray(bf.blocking_reasons) ? bf.blocking_reasons : [];
  const out = bf.outcomes || {};
  const outKeys = Object.keys(out);
  const tl = timeline || {};
  const decisions = Array.isArray(tl.decisions) ? tl.decisions : [];
  const tlTotal = tl.matched_filter ?? tl.total_for_date ?? 0;

  function setFilterAndFetch(v) {
    setTlFilter(v);
    if (onTimelineFilterChange) onTimelineFilterChange(v === 'all' ? '' : v);
  }
  function setCollapseAndFetch(v) {
    setTlCollapse(v);
    if (onTimelineCollapseChange) onTimelineCollapseChange(v);
  }
  return (
    <div style={{padding:'10px 12px',fontFamily:'var(--f-mono)',fontSize:10.5,color:'var(--fg-2)',overflowY:'auto',maxHeight:'100%'}}>
      {error && <div style={{color:'var(--neg)',marginBottom:8}}>error: {error}</div>}
      {loading && <div style={{color:'var(--fg-3)',marginBottom:8}}>loading…</div>}

      {/* ── Brain morning context ── */}
      <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase',margin:'2px 0 6px',display:'flex',alignItems:'center',justifyContent:'space-between'}}>
        <span>Brain · Morning Context</span>
        {brainData?.trade_date && <span style={{textTransform:'none',color:'var(--fg-4)',fontSize:9}}>{brainData.trade_date}</span>}
      </div>
      {(!brainData || !brainData.available) ? (
        <div style={{color:'var(--fg-4)',marginBottom:10,fontSize:10}}>
          No brain_state.json — engine not started or BRAIN_ENABLED=false.
          <br/>Daily feature builder must run for morning context to appear.
        </div>
      ) : (() => {
        const bd = brainData;
        const score = String(bd.day_score || 'UNKNOWN').toUpperCase();
        const scoreColor = { CALM:'var(--pos)', NEUTRAL:'var(--fg-3)', VOLATILE:'var(--warn)', AVOID:'var(--neg)', UNKNOWN:'var(--fg-4)' }[score] || 'var(--fg-4)';
        const sizeWarn = bd.size_multiplier != null && Number(bd.size_multiplier) < 1.0;
        const carryWarn = (bd.carry_consecutive_losses || 0) >= 2 || (bd.losing_streak_days || 0) >= 1;
        const fmtF = v => v != null ? Number(v).toFixed(4) : '—';
        const fmtPct = v => v != null ? (Number(v)>=0?'+':'') + (Number(v)*100).toFixed(2)+'%' : '—';
        return (
          <div style={{marginBottom:14,padding:'8px 10px',background:'var(--bg-2)',borderRadius:3,border:'1px solid var(--line-3)'}}>
            <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:'4px 10px',marginBottom:6}}>
              <div><span style={{color:'var(--fg-4)'}}>day_score</span> <span style={{color:scoreColor,fontWeight:700}}>{score}</span></div>
              <div><span style={{color:'var(--fg-4)'}}>conf</span> <span>{bd.day_score_confidence!=null?(Number(bd.day_score_confidence)*100).toFixed(0)+'%':'—'}</span></div>
              <div><span style={{color:'var(--fg-4)'}}>size_mult</span> <span style={{color:sizeWarn?'var(--warn)':'var(--pos)'}}>{bd.size_multiplier!=null?Number(bd.size_multiplier).toFixed(2)+'×':'—'}</span></div>
              <div><span style={{color:'var(--fg-4)'}}>carry</span> <span style={{color:carryWarn?'var(--warn)':'var(--fg-3)'}}>{bd.carry_consecutive_losses||0} consec · {bd.losing_streak_days||0}d streak</span></div>
              {bd.regime_rv20!=null && <div><span style={{color:'var(--fg-4)'}}>rv20</span> <span style={{color:Number(bd.regime_rv20)>0.018?'var(--neg)':Number(bd.regime_rv20)<0.010?'var(--pos)':'var(--warn)'}}>{fmtF(bd.regime_rv20)}</span></div>}
              {bd.regime_sma20_slope!=null && <div><span style={{color:'var(--fg-4)'}}>sma_slope</span> <span style={{color:Number(bd.regime_sma20_slope)>=0?'var(--pos)':'var(--neg)'}}>{fmtPct(bd.regime_sma20_slope)}</span></div>}
              {bd.regime_dist_sma20!=null && <div><span style={{color:'var(--fg-4)'}}>sma_dist</span> <span>{fmtPct(bd.regime_dist_sma20)}</span></div>}
              {bd.regime_60d_return!=null && <div><span style={{color:'var(--fg-4)'}}>60d_ret</span> <span style={{color:Number(bd.regime_60d_return)>=0?'var(--pos)':'var(--neg)'}}>{fmtPct(bd.regime_60d_return)}</span></div>}
            </div>
            {bd.day_score_reason && <div style={{fontSize:9,color:'var(--fg-4)',wordBreak:'break-all'}}>{bd.day_score_reason}</div>}
          </div>
        );
      })()}

      {/* Two distinct concepts:
           1. "Runtime config" = model the strategy_app container is loaded with
              right now. Would fire NEW trades on a new replay.
           2. "Trades shown produced by" = the model that wrote the events being
              rendered on the grid. Comes from active_run_id's metadata.
           When these differ, show a prominent mismatch warning so the operator
           doesn't confuse one for the other (the 2024-07-29 trap). */}
      <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase',margin:'2px 0 4px'}}>
        Runtime config <span style={{color:'var(--fg-4)',textTransform:'none'}}>(would fire NEW trades on next replay)</span>
      </div>
      <div style={{lineHeight:1.7}}>
        <div><span style={{color:'var(--fg-4)'}}>engine</span> <span style={{color:'var(--accent)'}}>{rc.engine || '—'}</span></div>
        {rc.strategy_profile_id && <div><span style={{color:'var(--fg-4)'}}>profile</span> <span style={{color:'var(--pos)'}}>{rc.strategy_profile_id}</span></div>}
        <div><span style={{color:'var(--fg-4)'}}>model_type</span> <span style={{color:rc.model_type==='option_pnl_v1' ? 'var(--pos)':'var(--fg-2)'}}>{rc.model_type || '—'}</span></div>
        {rc.recipe_id && <div><span style={{color:'var(--fg-4)'}}>recipe</span> <span style={{color:'var(--pos)'}}>{rc.recipe_id}</span> @ thr <span style={{color:'var(--fg-1)'}}>{rc.decision_threshold}</span></div>}
        <div><span style={{color:'var(--fg-4)'}}>model_run_id</span> {rc.model_run_id || '—'}</div>
        <div><span style={{color:'var(--fg-4)'}}>rollout</span> {rc.rollout_stage || '—'}</div>
        {rc.legacy_staged_model && (
          <div style={{color:'var(--fg-4)',fontSize:9.5,marginTop:4}}>
            (staged fallback loaded: <span style={{color:'var(--fg-3)'}}>{rc.legacy_staged_model.run_id}</span> — not firing)
          </div>
        )}
      </div>

      {/* Trades-shown provenance — uses the active_run_id of the displayed
          date (passed in diag.activeRunId from ReplayMonitorDark). */}
      <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase',margin:'14px 0 4px'}}>
        Trades shown were produced by <span style={{color:'var(--fg-4)',textTransform:'none'}}>(historical run for selected date)</span>
      </div>
      {(() => {
        const arid = diag.activeRunId || '—';
        const isDeterministic = rc.engine === 'deterministic';
        const isOptionPnl = typeof arid === 'string' && arid.startsWith('option_pnl_');
        const producedModel = arid === '—' ? '— (no stored run for this date)'
          : isDeterministic && rc.strategy_profile_id
            ? `deterministic · ${rc.strategy_profile_id}`
          : isOptionPnl ? 'option_pnl_v1 (bundle)'
          : 'staged_runtime_v1 (C1-family, futures-direction labels)';
        const runtimeRunId = rc.model_run_id || '';
        // Eval run UUID ≠ ML model_run_id for deterministic/playbook — do not warn.
        const mismatch = !isDeterministic && arid !== '—' && arid !== runtimeRunId;
        return (
          <div style={{lineHeight:1.7}}>
            <div><span style={{color:'var(--fg-4)'}}>run_id</span> <span style={{color:isOptionPnl?'var(--pos)':'var(--warn)'}}>{arid}</span></div>
            <div><span style={{color:'var(--fg-4)'}}>model</span> <span style={{color:isOptionPnl?'var(--pos)':'var(--warn)'}}>{producedModel}</span></div>
            {mismatch && (
              <div style={{marginTop:6,padding:'6px 8px',background:'rgba(245,165,36,0.10)',border:'1px solid rgba(245,165,36,0.35)',borderRadius:2,color:'var(--warn)',fontSize:10}}>
                ⚠ Trades shown ≠ runtime config. The runtime model above would produce
                <em> different</em> trades. To see what the new model would do for this date,
                trigger a fresh replay (POST /api/historical/replay/generate).
              </div>
            )}
            {!mismatch && arid !== '—' && (
              <div style={{marginTop:4,color:'var(--pos)',fontSize:10}}>✓ Trades shown match runtime config</div>
            )}
          </div>
        );
      })()}

      <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase',margin:'14px 0 4px'}}>
        Available models <span style={{color:'var(--fg-4)'}}>({models.length})</span>
      </div>
      {models.length === 0 ? (
        <div style={{color:'var(--fg-4)'}}>no published models found</div>
      ) : (
        <div style={{maxHeight:120,overflowY:'auto',border:'1px solid var(--line-3)',borderRadius:2,padding:'4px 6px'}}>
          {models.slice(0,30).map((m,i) => {
            const active = m.run_id && rc.model_run_id && m.run_id === rc.model_run_id;
            return (
              <div key={i} style={{padding:'2px 0',color:active?'var(--accent)':'var(--fg-2)'}}>
                {active ? '●' : '○'} <span>{m.run_id || '?'}</span>
                {m.model_group && <span style={{color:'var(--fg-4)',marginLeft:6}}>{m.model_group}</span>}
              </div>
            );
          })}
        </div>
      )}

      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',margin:'14px 0 4px'}}>
        <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase'}}>
          Blocker funnel <span style={{color:'var(--fg-4)'}}>{date || '—'}</span>
        </div>
        {onRefresh && <button className="t-btn ghost sm" onClick={onRefresh} title="Reload diagnostics">refresh</button>}
      </div>
      {!bf.narrative ? (
        <div style={{color:'var(--fg-4)'}}>no decision traces loaded — pick a date</div>
      ) : (
        <>
          <div style={{color:'var(--fg-1)',marginBottom:8,lineHeight:1.5}}>{bf.narrative}</div>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10}}>
            <div>
              <div style={{color:'var(--fg-4)',fontSize:9,marginBottom:2}}>outcomes</div>
              {outKeys.length === 0 ? <div style={{color:'var(--fg-4)'}}>—</div> :
                outKeys.map(k => (
                  <div key={k}><span style={{color:'var(--fg-4)'}}>{k}</span> <span style={{color:'var(--fg-1)'}}>{out[k]}</span></div>
                ))}
            </div>
            <div>
              <div style={{color:'var(--fg-4)',fontSize:9,marginBottom:2}}>primary blocker gates</div>
              {gates.length === 0 ? <div style={{color:'var(--fg-4)'}}>—</div> :
                gates.map((g,i) => (
                  <div key={i}><span style={{color:'var(--fg-4)'}}>{g.gate}</span> <span style={{color:'var(--fg-1)'}}>{g.count}</span></div>
                ))}
            </div>
          </div>
          <div style={{marginTop:10}}>
            <div style={{color:'var(--fg-4)',fontSize:9,marginBottom:2}}>top reason codes (first non-pass gate)</div>
            {reasons.length === 0 ? <div style={{color:'var(--fg-4)'}}>—</div> :
              reasons.map((r,i) => (
                <div key={i}><span style={{color:'var(--fg-4)'}}>{r.reason_code}</span> <span style={{color:'var(--fg-1)'}}>{r.count}</span></div>
              ))}
          </div>
        </>
      )}

      {/* Per-minute decision timeline — what happened at each snapshot */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',margin:'18px 0 4px'}}>
        <div style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase'}}>
          Per-minute decisions <span style={{color:'var(--fg-4)'}}>({decisions.length} of {tlTotal})</span>
        </div>
        <div style={{display:'flex',gap:4,alignItems:'center'}}>
          {['all','blocked','hold','entry_taken'].map(v => (
            <button key={v}
                    className={'t-btn ghost sm' + (tlFilter===v?' active':'')}
                    onClick={()=>setFilterAndFetch(v)}
                    style={{fontSize:9.5, padding:'2px 6px',
                            color: tlFilter===v ? 'var(--accent)' : 'var(--fg-3)',
                            borderColor: tlFilter===v ? 'var(--accent)' : undefined}}>
              {v}
            </button>
          ))}
          {/* "collapse" merges consecutive rows with bit-identical Stage-1 output
              into a single row — exposes the "Stage-1 stuck for N minutes" signal */}
          <button className={'t-btn ghost sm' + (tlCollapse?' active':'')}
                  onClick={()=>setCollapseAndFetch(!tlCollapse)}
                  title="merge consecutive rows with identical Stage-1 output"
                  style={{fontSize:9.5, padding:'2px 6px', marginLeft:6,
                          color: tlCollapse ? 'var(--accent)' : 'var(--fg-3)',
                          borderColor: tlCollapse ? 'var(--accent)' : undefined}}>
            collapse
          </button>
        </div>
      </div>
      {decisions.length === 0 ? (
        <div style={{color:'var(--fg-4)'}}>{tl.traces_path_exists === false ? 'no decision_traces.jsonl for this mode' : 'no rows match the current filter'}</div>
      ) : (
        <div style={{maxHeight:260,overflowY:'auto',border:'1px solid var(--line-3)',borderRadius:2}}>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:10}}>
            <thead style={{position:'sticky',top:0,background:'var(--bg-2)'}}>
              <tr style={{color:'var(--fg-4)',fontSize:9}}>
                <th style={{textAlign:'left',padding:'3px 6px',width:46}}>time</th>
                <th style={{textAlign:'left',padding:'3px 6px',width:76}}>outcome</th>
                <th style={{textAlign:'left',padding:'3px 6px'}}>gate · reason</th>
                <th style={{textAlign:'right',padding:'3px 6px',width:56}}>entry</th>
                <th style={{textAlign:'left',padding:'3px 6px',width:70}}>s1 hash</th>
                <th style={{textAlign:'right',padding:'3px 6px',width:48}}>s1 nn</th>
                <th style={{textAlign:'right',padding:'3px 6px',width:56}}>recipe</th>
                <th style={{textAlign:'left',padding:'3px 6px',width:80}}>regime</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d,i) => {
                const s1 = _stageDiag(d, 'stage1');
                const s1Title = [
                  `stage1 input_hash=${s1.input_hash || '—'}`,
                  `features=${s1.feature_count ?? '—'}`,
                  `non_null=${s1.non_null_count ?? '—'}`,
                  `missing=${s1.missing_count ?? '—'}`,
                  `output=${s1.output_prob ?? (d.metrics||{}).entry_prob ?? '—'}`,
                ].join(' · ');
                const runMins = d.run_minutes || 1;
                const isRun = runMins > 1;
                const timeCell = isRun
                  ? <span title={`${runMins} consecutive snapshots with identical Stage-1 entry_prob`}>
                      {d.time}<span style={{color:'var(--fg-4)'}}>–{d.time_end}</span>
                      <span style={{color:'var(--accent)',marginLeft:4,fontSize:9}}>×{runMins}</span>
                    </span>
                  : (d.time || '—');
                return (
                  <tr key={i} style={{borderTop:'1px solid var(--line-3)',background:isRun?'rgba(245,165,36,0.04)':'transparent'}}>
                    <td style={{padding:'2px 6px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',whiteSpace:'nowrap'}}>{timeCell}</td>
                    <td style={{padding:'2px 6px',color:_outcomeColor(d.outcome)}}>{d.outcome || '—'}</td>
                    <td style={{padding:'2px 6px',color:'var(--fg-2)'}}>
                      {d.blocker_gate ? <span style={{color:'var(--fg-3)'}}>{d.blocker_gate}</span> : <span style={{color:'var(--fg-4)'}}>—</span>}
                      {d.reason_code && <span style={{color:'var(--fg-1)',marginLeft:6}}>{d.reason_code}</span>}
                    </td>
                    <td title={s1Title} style={{padding:'2px 6px',textAlign:'right',fontFamily:'var(--f-mono)',color:'var(--fg-3)'}}>{_fmtProb((d.metrics||{}).entry_prob)}</td>
                    <td title={s1Title} style={{padding:'2px 6px',fontFamily:'var(--f-mono)',color:s1.input_hash?'var(--info)':'var(--fg-4)'}}>{_shortHash(s1.input_hash)}</td>
                    <td title={s1Title} style={{padding:'2px 6px',textAlign:'right',fontFamily:'var(--f-mono)',color:'var(--fg-4)'}}>{s1.non_null_count ?? '—'}</td>
                    <td style={{padding:'2px 6px',textAlign:'right',fontFamily:'var(--f-mono)',color:'var(--fg-3)'}}>{_fmtProb((d.metrics||{}).recipe_prob)}</td>
                    <td style={{padding:'2px 6px',color:'var(--fg-4)',fontSize:9}}>{d.regime || '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Tape: unified trades + signals ────────────────────────────────────────
function Tape({ session, trades, signals, selectedTrade, onSelectTrade, flashId, diag }) {
  const [filter, setFilter] = _s('all');
  const rows = _m(() => {
    const out = [];
    if (filter !== 'signals') trades.forEach(t => out.push({ kind: 'trade', sortIdx: t.exitIdx, ...t }));
    if (filter !== 'trades')  signals.forEach(s => out.push({ kind: 'signal', sortIdx: s.idx, ...s }));
    return out.sort((a, b) => b.sortIdx - a.sortIdx);
  }, [trades, signals, filter]);

  return (
    <div className="t-tape-panel">
      <div className="t-panel-head" style={{paddingLeft:0}}>
        <div className="t-tape-tabs">
          <button className={filter==='all'?'active':''} onClick={()=>setFilter('all')}>Tape <span className="count">{trades.length+signals.length}</span></button>
          <button className={filter==='trades'?'active':''} onClick={()=>setFilter('trades')}>Trades <span className="count">{trades.length}</span></button>
          <button className={filter==='signals'?'active':''} onClick={()=>setFilter('signals')}>Signals <span className="count">{signals.length}</span></button>
          {diag && <button className={filter==='diag'?'active':''} onClick={()=>setFilter('diag')}>Diag</button>}
        </div>
      </div>
      {filter === 'diag' ? (
        <div className="t-panel-body" style={{padding:0}}><DiagPanel diag={diag}/></div>
      ) : (
      <div className="t-panel-body">
        <table className="t-tape-table">
          <thead>
            <tr>
              <th style={{width:58}}>Time</th>
              <th style={{width:36}}>Type</th>
              <th>Strategy</th>
              <th style={{width:42}}>Dir</th>
              <th style={{width:86}}>Contract</th>
              <th className="r" style={{width:70}}>Entry</th>
              <th className="r" style={{width:70}}>Exit</th>
              <th className="r" style={{width:58}}>P&amp;L</th>
              <th className="r" style={{width:36}}>Hold</th>
              <th style={{width:80}}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              if (r.kind === 'trade') {
                const sel = selectedTrade?.id === r.id;
                return (
                  <tr key={'t'+r.id} className={`${sel?'selected':''} ${r.id===flashId?'t-tape-flash':''}`}
                      onClick={() => onSelectTrade(r)}>
                    <td className="muted" style={{whiteSpace:'nowrap',fontFamily:'var(--f-mono)',fontSize:'9px'}}>
                      {_barLabel(session, r.entryIdx)}<span style={{opacity:0.4}}> → </span>{_barLabel(session, r.exitIdx)}
                    </td>
                    <td>
                      <span className={`t-tape-type-dot ${(r.dir||'').toLowerCase()} fired`}/>
                      <span className="muted" style={{fontSize:'9px'}}>FILL</span>
                    </td>
                    <td>{r.strat}</td>
                    <td>
                      <span className={`t-dir ${r.dir}`} title="Chart delta bias">
                        {r.legDir || r.dir}
                        {r.positionSide === 'LONG' ? ' · BUY' : r.positionSide === 'SHORT' ? ' · SELL' : ''}
                      </span>
                    </td>
                    <td style={{fontFamily:'var(--f-mono)',fontSize:'10px',whiteSpace:'nowrap'}}>
                      {r.strike ? (<>
                        <span>{Math.round(r.strike)}</span>
                        {r.optionType && <span className={`t-opt-tag ${r.optionType.toLowerCase()}`} style={{marginLeft:4,padding:'0 4px',borderRadius:2,fontSize:'9px',background:r.optionType==='CE'?'rgba(25,195,125,0.18)':'rgba(242,60,74,0.18)',color:r.optionType==='CE'?'var(--pos)':'var(--neg)'}}>{r.optionType}</span>}
                      </>) : <span className="muted">—</span>}
                    </td>
                    <td className="r">{(r.entryPx||0).toFixed(0)}</td>
                    <td className="r">{(r.exitPx||0).toFixed(0)}</td>
                    <td className={`r ${(r.pnlPct||0)>=0?'t-pos':'t-neg'}`}>{TC.fmtPct(r.pnlPct||0,2)}</td>
                    <td className="r muted">{r.heldBars ?? '—'}b</td>
                    <td className="muted" style={{fontSize:'9px'}}>{(r.exitReason||'').replace('_',' ')}</td>
                  </tr>
                );
              }
              const dir = (r.dir || r.direction || 'LONG').toLowerCase();
              return (
                <tr key={'s'+i} className={r.fired?'':'signal-held'}>
                  <td className="muted">{_barLabel(session, r.idx)}</td>
                  <td>
                    <span className={`t-tape-type-dot ${dir} ${r.fired?'fired':'held'}`}/>
                    <span className="muted" style={{fontSize:'9px',color:r.fired?'var(--fg-2)':'var(--fg-4)'}}>{r.fired?'SIG':'HLD'}</span>
                  </td>
                  <td>{r.strat || r.strategy_name || '—'}</td>
                  <td className="muted" style={{fontSize:'9px'}}>
                    {(r.dir === 'CE' || r.dir === 'PE') ? r.dir : (r.legDir || r.dir || '—')}
                    {r.fired && r.positionSide === 'LONG' ? ' · BUY' : r.fired && r.positionSide === 'SHORT' ? ' · SELL' : ''}
                  </td>
                  <td className="muted">—</td>
                  <td className="r muted">—</td><td className="r muted">—</td><td className="r muted">—</td><td className="r muted">—</td>
                  <td className="muted" style={{fontSize:'9px'}}>{r.reason || r.hold_reason || '—'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}
    </div>
  );
}

// ── Trade Inspector ──────────────────────────────────────────────────────
function TradeInspector({ session, trade }) {
  if (!trade) {
    return (
      <div className="t-inspector">
        <div style={{padding:'14px 12px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',fontSize:11}}>
          Click a trade to inspect.
        </div>
      </div>
    );
  }

  const outCls = trade.exitReason === 'TARGET_HIT' ? 'pos' : trade.exitReason === 'STOP_HIT' ? 'neg' : 'warn';
  // plannedPct is kept as a FRACTION (e.g. 0.05 = +5%) to match trade.pnlPct units;
  // it's rendered via TC.fmtPct which multiplies by 100 at display time.
  const plannedPct = ((trade.targetPx - trade.entryPx) / trade.entryPx) * (trade.dir === 'SHORT' ? -1 : 1);

  const probs = trade.probs || {
    entry_prob:    { v: trade.conf, gate: 0.60, label: 'Entry gate' },
    trade_prob:    { v: trade.conf * 0.92, gate: 0.55, label: 'Trade gate' },
    ce_prob:       { v: trade.dir === 'LONG' ? 0.65 : 0.28, label: 'CE bias' },
    pe_prob:       { v: trade.dir === 'LONG' ? 0.28 : 0.65, label: 'PE bias' },
    recipe_prob:   { v: trade.conf * 0.94, label: 'Recipe prob' },
    recipe_margin: { v: trade.conf * 0.42, label: 'Recipe margin' },
  };

  const confluence = trade.confluence || [];
  const counterfactuals = trade.counterfactuals || [
    { label: 'Half size',       delta: -(trade.pnlPct||0)/2, note: 'Linear PnL scaling' },
    { label: 'Skip this trade', delta: -(trade.pnlPct||0),   note: 'Realized PnL forfeited' },
  ];

  // synthetic price path for risk envelope
  const pathPoints = _m(() => {
    const n = 10; let _s2 = 31;
    const rnd = () => { _s2 = (_s2*9301+49297)%233280; return _s2/233280; };
    return Array.from({length: n+1}, (_, i) => {
      const t = i / n;
      const u = t*t*(3-2*t);
      const mid = trade.entryPx + (trade.exitPx - trade.entryPx) * u;
      const dev = (rnd()-0.5) * Math.abs(trade.stopPx - trade.targetPx) * 0.08;
      return { x: t, p: mid + dev };
    });
  }, [trade]);

  return (
    <div className="t-inspector">
      <div className="t-inspector-head">
        <span className={`t-dir ${trade.dir}`} style={{fontSize:'11px',padding:'3px 8px'}}>{trade.dir}</span>
        <div>
          <div className="t-inspector-id">{trade.id} <span style={{color:'var(--fg-3)',fontWeight:400,fontSize:10.5}}>{trade.strat}</span></div>
          <div className="t-inspector-meta">
            <span>{_barLabel(session,trade.entryIdx)} → {_barLabel(session,trade.exitIdx)}</span>
            <span>·</span><span>{trade.heldBars}b</span>
            <span>·</span><span>{trade.regime}</span>
          </div>
        </div>
        <span className={`t-chip ${outCls}`}>● {(trade.exitReason||'').replace('_',' ')}</span>
      </div>
      <div className="t-inspector-body">
        {/* Outcome */}
        <div className="t-outcome-grid">
          <div className="t-outcome-cell big">
            <div className="k">Realized P&amp;L</div>
            <div className={`v ${(trade.pnlPct||0)>=0?'pos':'neg'}`}>{TC.fmtPct(trade.pnlPct||0,2)}</div>
            {plannedPct !== 0 && <div className="sub">vs planned {TC.fmtPct(plannedPct,2)} · realized/planned {(Math.abs(trade.pnlPct||0)/Math.abs(plannedPct)).toFixed(2)}×</div>}
          </div>
          <div className="t-outcome-cell"><div className="k">Entry</div><div className="v">{(trade.entryPx||0).toFixed(2)}</div></div>
          <div className="t-outcome-cell"><div className="k">Exit</div><div className="v">{(trade.exitPx||0).toFixed(2)}</div></div>
          <div className="t-outcome-cell"><div className="k">Conf</div><div className="v">{((trade.conf||0)*100).toFixed(0)}%</div></div>
        </div>

        {/* Probability stack */}
        <div className="t-section-head">Why it fired</div>
        <div className="t-prob-stack">
          {Object.values(probs).map((p, i) => <ProbRow key={i} {...p} kind={i < 2 ? 'gate' : 'neutral'}/>)}
        </div>

        {/* Confluence */}
        {confluence.length > 0 && <>
          <div className="t-section-head">Confluence</div>
          <div className="t-confluence-list">
            {confluence.map((c, i) => <span key={i} className="t-confluence-chip">{c}</span>)}
          </div>
        </>}

        {/* Risk envelope */}
        <div className="t-section-head">Risk envelope</div>
        <RiskEnvelope trade={trade} path={pathPoints} session={session}/>

        {/* Rationale */}
        <div className="t-section-head">Entry · Exit rationale</div>
        <div className="t-rationale">
          <div className="t-rationale-block entry">
            <div className="heading"><span style={{color:'var(--pos)'}}>● Entry</span><span style={{color:'var(--fg-3)'}}>{_barLabel(session,trade.entryIdx)}</span></div>
            <div className="body">{trade.entryDetail}</div>
          </div>
          <div className={`t-rationale-block exit ${trade.exitReason==='TARGET_HIT'?'target':''}`}>
            <div className="heading"><span style={{color:trade.exitReason==='TARGET_HIT'?'var(--pos)':'var(--neg)'}}>● Exit · {(trade.exitReason||'').replace('_',' ')}</span></div>
            <div className="body">{trade.exitDetail}</div>
          </div>
        </div>

        {/* Counterfactuals */}
        <div className="t-section-head">What-if</div>
        <div className="t-cf-ribbon">
          {counterfactuals.map((cf, i) => {
            const d = typeof cf.delta === 'number' ? cf.delta : 0;
            const pct = Math.min(100, Math.abs(d) / 0.4 * 50);
            const cls = d > 0.001 ? 'pos' : d < -0.001 ? 'neg' : 'zero';
            return (
              <div key={i} className="t-cf-row">
                <div className="scen">{cf.label}<span className="note">{cf.note}</span></div>
                <div className={`delta ${cls}`}>{d===0?'±0.00':TC.fmtPct(d,2)}</div>
                <div className="delta-bar">
                  <span className="zero-mark"/>
                  <span className={`fill ${cls==='neg'?'neg':''}`} style={{left:d>=0?'50%':(50-pct)+'%',width:pct+'%'}}/>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ProbRow({ v, gate, label, kind }) {
  v = Math.max(0, Math.min(1, v || 0));
  const cleared = gate != null ? v >= gate : null;
  const fillCls = kind === 'gate' ? (cleared ? 'pass' : 'fail') : 'neut';
  const valCls  = fillCls;
  return (
    <div className="t-prob-row">
      <div className="label">
        {label}
        {gate != null && <span className="gate">gate {gate.toFixed(2)} · {cleared ? '✓ pass' : '✗ fail'}</span>}
      </div>
      <div className="t-prob-bar">
        <div className={`fill ${fillCls}`} style={{width:(v*100)+'%'}}/>
        {gate != null && <div className="gate-mark" style={{left:(gate*100)+'%'}}/>}
      </div>
      <div className={`val ${valCls}`}>{v.toFixed(2)}</div>
    </div>
  );
}

function RiskEnvelope({ trade, path, session }) {
  const W = 320, H = 110;
  const PAD = { l: 8, r: 54, t: 12, b: 18 };
  const iW = W - PAD.l - PAD.r, iH = H - PAD.t - PAD.b;
  const prices = [trade.entryPx, trade.stopPx, trade.targetPx, ...path.map(p => p.p)];
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const pad = (hi - lo) * 0.15;
  const pMin = lo - pad, pMax = hi + pad;
  const yOf = p => PAD.t + (pMax - p) / (pMax - pMin) * iH;
  const xOf = t => PAD.l + t * iW;
  const d = path.map((p, i) => `${i===0?'M':'L'} ${xOf(p.x).toFixed(1)} ${yOf(p.p).toFixed(1)}`).join(' ');
  return (
    <div className="t-risk-mini">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <rect x={PAD.l} y={Math.min(yOf(trade.stopPx),yOf(trade.entryPx))} width={iW} height={Math.abs(yOf(trade.stopPx)-yOf(trade.entryPx))} fill="var(--neg)" fillOpacity="0.05"/>
        <rect x={PAD.l} y={Math.min(yOf(trade.targetPx),yOf(trade.entryPx))} width={iW} height={Math.abs(yOf(trade.targetPx)-yOf(trade.entryPx))} fill="var(--pos)" fillOpacity="0.05"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.stopPx)}   y2={yOf(trade.stopPx)}   className="stop-line"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.targetPx)} y2={yOf(trade.targetPx)} className="target-line"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.entryPx)}  y2={yOf(trade.entryPx)}  className="entry-line"/>
        <text x={W-PAD.r+3} y={yOf(trade.stopPx)+3}   className="level-label stop"  >{trade.stopPx.toFixed(0)}</text>
        <text x={W-PAD.r+3} y={yOf(trade.targetPx)+3} className="level-label target">{trade.targetPx.toFixed(0)}</text>
        <text x={W-PAD.r+3} y={yOf(trade.entryPx)+3}  className="level-label"       >{trade.entryPx.toFixed(0)}</text>
        <path d={d} className="price-path"/>
        <circle cx={xOf(0)} cy={yOf(trade.entryPx)} r="3.5" className="entry-dot"/>
        <circle cx={xOf(1)} cy={yOf(trade.exitPx)}  r="4"   className="exit-dot"/>
        <text x={xOf(0)}   y={H-4} fontFamily="var(--f-mono)" fontSize="8" fill="var(--fg-3)" textAnchor="start">{_barLabel(session,trade.entryIdx)}</text>
        <text x={xOf(0.99)} y={H-4} fontFamily="var(--f-mono)" fontSize="8" fill="var(--fg-3)" textAnchor="end">{_barLabel(session,trade.exitIdx)}</text>
      </svg>
    </div>
  );
}

// ── Log strip ─────────────────────────────────────────────────────────────
function LogStrip({ session, alerts, wsStatus }) {
  const items = (alerts || []).slice(-4);
  return (
    <div className="t-log-strip">
      <span className="t-chip" style={{fontSize:'8.5px'}}>LOG</span>
      <div className="t-log-feed">
        {items.length === 0
          ? <span style={{color:'var(--fg-4)'}}>No alerts</span>
          : items.map((a, i) => (
            <span key={i} className="t-log-item">
              <span className="t">{_barLabel(session, a.idx ?? 0)}</span>
              <span className={`sev ${a.sev || a.level || 'info'}`}/>
              <span className="msg">{a.msg || a.message}</span>
            </span>
          ))
        }
      </div>
      <div className="t-log-perf">
        <span><span className="k">ws</span><span className={wsStatus==='connected'?'ok':''}>{wsStatus}</span></span>
        <span><span className="k">tbl</span><span className="ok">{(alerts||[]).length}</span></span>
      </div>
    </div>
  );
}

// ── LiveMonitorDark — main component ────────────────────────────────────
function LiveMonitorDark({ onModeSwitch, onKillClick }) {
  const [session,       setSession]       = _s(null);
  const [upToIdx,       setUpToIdx]       = _s(0);
  const [livePrice,     setLivePrice]     = _s(null);
  const [wsStatus,      setWsStatus]      = _s('connecting');
  const [selectedTrade, setSelectedTrade] = _s(null);
  const [flashId,       setFlashId]       = _s(null);
  const [flashIdx,      setFlashIdx]      = _s(null);
  const [runtimeConfig, setRuntimeConfig] = _s(null);
  const [availableModels, setAvailableModels] = _s([]);
  const wsRef         = _r(null);
  const sessionRef    = _r(null);
  const prevIdxRef    = _r(null);
  _e(() => { sessionRef.current = session; }, [session]);

  // Diagnostics: in live mode we surface current model + available models.
  // Blocker funnel needs a date and is replay-only.
  _e(() => {
    let alive = true;
    fetch('/api/strategy/current/state?mode=live&latest_n=0')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)))
      .then(s => { if (!alive) return; setRuntimeConfig(s?.runtime_config || null); setAvailableModels(Array.isArray(s?.available_models) ? s.available_models : []); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  _e(() => {
    const ws = TC.makeMonitorWS(
      () => ({ action: 'subscribe', mode: 'live' }),
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            setSession(msg.session);
            setUpToIdx(msg.up_to_idx);
            if (msg.live_price != null) setLivePrice(msg.live_price);
            prevIdxRef.current = msg.up_to_idx;
          } else if (msg.type === 'tick') {
            const newIdx = msg.up_to_idx;
            const sess = sessionRef.current;
            if (sess && prevIdxRef.current !== null && newIdx > prevIdxRef.current) {
              const filled = sess.trades.find(t => t.exitIdx > prevIdxRef.current && t.exitIdx <= newIdx);
              if (filled) {
                const bridged = _bridgeTrade(filled);
                setFlashId(bridged.id);
                setFlashIdx(filled.exitIdx);
                setSelectedTrade(bridged);
                setTimeout(() => { setFlashId(null); setFlashIdx(null); }, 1400);
              }
            }
            prevIdxRef.current = newIdx;
            setUpToIdx(newIdx);
            if (msg.live_price != null) setLivePrice(msg.live_price);
          }
        },
      }
    );
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  // keyboard nav
  _e(() => {
    const trades = session ? session.trades.filter(t => t.exitIdx <= upToIdx).map(_bridgeTrade) : [];
    const fn = e => {
      if (e.target.tagName === 'INPUT') return;
      if (e.key === 'j' || e.key === 'J') {
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i > 0) setSelectedTrade(trades[i - 1]);
      } else if (e.key === 'k' || e.key === 'K') {
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i >= 0 && i < trades.length - 1) setSelectedTrade(trades[i + 1]);
      } else if ((e.metaKey || e.ctrlKey) && e.key === '.') {
        e.preventDefault(); onKillClick();
      }
    };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [session, upToIdx, selectedTrade, onKillClick]);

  if (!session) {
    return (
      <div className="cockpit" style={{gridTemplateRows:'1fr'}}>
        <div className="t-loading">
          <span>{wsStatus === 'connecting' ? 'Connecting…' : wsStatus === 'disconnected' ? 'Reconnecting…' : 'Loading session…'}</span>
        </div>
      </div>
    );
  }

  const candles        = session.candles || [];
  const rawTrades      = (session.trades || []).filter(t => t.exitIdx <= upToIdx);
  const trades         = rawTrades.map(_bridgeTrade);
  const signals        = (session.signals || []).filter(s => s.idx <= upToIdx).slice(-60).reverse();
  const strategies     = _makeStrategies(trades);
  const quote          = _makeQuote(session, upToIdx);
  const sessionPnl     = rawTrades.reduce((a, t) => a + (t.pnlPct || 0), 0);
  const wins           = rawTrades.filter(t => (t.pnlPct || 0) > 0).length;
  const winRate        = rawTrades.length ? Math.round(wins / rawTrades.length * 100) : 0;
  const regime         = session.regime || (trades[0]?.regime) || '—';
  const engine         = session.engine || 'ML_PURE';

  // Default to latest trade in inspector
  const displayTrade   = selectedTrade || (trades.length > 0 ? trades[0] : null);

  return (
    <div className="cockpit">
      <TickerBar quote={quote} instrument={session.instrument}/>
      <StatusBar
        sessionPnl={sessionPnl} tradesCount={trades.length} winRate={winRate}
        regime={regime} engine={engine} ws={wsStatus}
        onModeSwitch={onModeSwitch} onHaltClick={onKillClick}
      />
      <div className="t-workspace">
        <EngineRoster strategies={strategies} dailyRisk={Math.abs(Math.min(0, sessionPnl))}/>

        <div className="t-center">
          <div className="t-chart-panel">
            <div className="t-panel-head">
              <div className="t-panel-title">
                {session.instrument} · 1m <span className="count">{Math.min(upToIdx+1,candles.length)}/{candles.length} bars</span>
              </div>
              <div className="t-panel-actions">
                {displayTrade && <span className="t-chip amber">● {displayTrade.id}</span>}
              </div>
            </div>
            <TermChart
              session={session} candles={candles} trades={trades}
              selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
              upToIdx={upToIdx} flashIdx={flashIdx}
            />
          </div>
          <Tape
            session={session} trades={[...trades].reverse()} signals={signals}
            selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
            flashId={flashId}
            diag={{ runtimeConfig, availableModels, blockerFunnel: null,
                    loading: false, error: '', date: null, onRefresh: null }}
          />
        </div>

        <div className="t-rail right" style={{display:'flex',flexDirection:'column'}}>
          <div className="t-panel-head">
            <div className="t-panel-title">Trade Inspector</div>
            <div className="t-panel-actions">
              <button className="t-btn ghost sm" title="Prev trade (K)"
                onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i>0)setSelectedTrade(trades[i-1]); }}>K↑</button>
              <button className="t-btn ghost sm" title="Next trade (J)"
                onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i<trades.length-1)setSelectedTrade(trades[i+1]); }}>↓J</button>
            </div>
          </div>
          <div style={{flex:1,overflowY:'auto',minHeight:0}}>
            <TradeInspector session={session} trade={displayTrade}/>
          </div>
        </div>
      </div>
      <LogStrip session={session} alerts={session.alerts} wsStatus={wsStatus}/>
    </div>
  );
}

// ── Replay Ticker Bar ─────────────────────────────────────────────────────
function ReplayTickerBar({ date, vtLabel, instrument, isPlaying }) {
  const [clock, setClock] = _s(TC.fmtClock(new Date()));
  _e(() => { const id = setInterval(() => setClock(TC.fmtClock(new Date())), 1000); return () => clearInterval(id); }, []);
  return (
    <div className="ticker-bar">
      <div className="t-brand">
        <span className="t-brand-mark"/>
        <b>QUANT</b><span>OPS</span>
        <span className="ver">replay</span>
      </div>
      <div className="ticker-pairs">
        {instrument && <div className="pair"><span className="lbl">{instrument}</span></div>}
        <div className="pair">
          <span className="lbl">Date</span>
          <span className="val">{date || '—'}</span>
        </div>
        <div className="pair">
          <span className="lbl">Time</span>
          <span className="val" style={{color:'var(--info)'}}>{vtLabel || '—'}</span>
        </div>
        <div className="pair">
          <span className={`val ${isPlaying ? 'pos' : 'warn'}`} style={{fontSize:'10px'}}>
            {isPlaying ? '▶ PLAYING' : '⏸ PAUSED'}
          </span>
        </div>
      </div>
      <div className="ticker-clock">
        <span style={{color:'var(--fg-4)',fontSize:'9px',letterSpacing:'0.10em',textTransform:'uppercase',marginRight:4}}>IST</span>
        <span>{clock}</span>
      </div>
    </div>
  );
}

// ── Replay Status Bar ─────────────────────────────────────────────────────
//
// C1 model training windows (per ml_pipeline_2/docs/training/MODEL_STATE_20260514.md):
//   train:   2020-08-03 → 2024-04-30  — model SAW these dates during training
//   valid:   2024-05-01 → 2024-07-31  — hyperparameter-tuning data
//   holdout: 2024-08-01 → 2024-10-31  — truly out-of-sample (never seen)
//
// Surfacing these windows in the date picker prevents celebrating in-sample
// numbers as evidence of edge. See PROJECT_PLAN.md §14.
const C1_TRAIN_END   = "2024-04-30";
const C1_VALID_END   = "2024-07-31";
const C1_HOLDOUT_END = "2024-10-31";

function _windowOf(d) {
  if (!d) return "?";
  if (d <= C1_TRAIN_END)   return "train";
  if (d <= C1_VALID_END)   return "valid";
  if (d <= C1_HOLDOUT_END) return "OOS";
  return "post";
}

function _fmtDateOption(d, tradeCounts, modelsByDate) {
  const n = (tradeCounts && tradeCounts[d]) || 0;
  const win = _windowOf(d);
  // Mark window so operators can tell at a glance whether they're looking at
  // in-sample (train), light-contamination (valid), or true out-of-sample (OOS) data.
  const tag = win === "train" ? "● train" : win === "valid" ? "◐ valid" : win === "OOS" ? "○ OOS  " : "  post ";
  // Explicit zero-trade marker so operators don't have to scroll the dropdown to
  // figure out which dates have replay data and which are empty by model behavior.
  const tradeStr = n > 0 ? `${String(n).padStart(3,' ')} trades` : "   — empty";
  // Model tag — derived from the latest run_id for this date so the operator
  // can see at a glance whether this date's trades came from C1 (old futures
  // direction) or a new option-P&L bundle. Padded to keep columns aligned.
  const m = modelsByDate && modelsByDate[d];
  let modelTag = "         ";
  if (m && m.family) {
    if (m.family === "OPT_PNL") {
      modelTag = ` · ${(m.recipe || "OPT_PNL").padEnd(10)}`;
    } else {
      modelTag = ` · ${(m.family || "?").padEnd(10)}`;
    }
  }
  return `${tag} · ${d} · ${tradeStr}${modelTag}`;
}

function ReplayStatusBar({ sessionPnl, tradesCount, winRate, isPlaying, speed, upToIdx,
  total, availableDates, tradeCounts, modelsByDate, replayDate, replayRunId, onPlay, onPause, onSpeed, onScrub, onScrubEnd,
  onReset, onDateChange, onModeSwitch, ws, datesLoading }) {
  const pnlCls = sessionPnl >= 0 ? 'pos' : 'neg';
  const pct = total > 0 ? ((upToIdx + 1) / total * 100).toFixed(0) : 0;
  const selStyle = { fontFamily:'var(--f-mono)', fontSize:'10px', background:'var(--bg-2)',
    color:'var(--fg-1)', border:'1px solid var(--line-2)', borderRadius:'var(--r-2)',
    padding:'0 6px', height:20 };
  return (
    <div className="t-status-bar">
      <div className="t-mode-toggle">
        <button onClick={() => onModeSwitch('live')}><span className="dot live"/>Live</button>
        <button className="active"><span className="dot replay"/>Replay</button>
        <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
      </div>
      <div style={{display:'flex',alignItems:'center',gap:5,padding:'0 10px',flex:1,overflow:'hidden',minWidth:0}}>
        {isPlaying
          ? <button className="t-btn sm" onClick={onPause}>⏸</button>
          : <button className="t-btn sm" style={{background:'var(--accent)',color:'#1a1100',borderColor:'var(--accent)'}} onClick={onPlay}>▶</button>
        }
        <button className="t-btn sm ghost" onClick={onReset} title="Reset to start">⟲</button>
        <div style={{display:'flex',gap:2,flexShrink:0}}>
          {[1,2,4,8,16].map(s => (
            <button key={s} className="t-btn sm ghost"
              style={speed===s?{background:'var(--bg-4)',borderColor:'var(--line-3)',color:'var(--fg-1)'}:{}}
              onClick={() => onSpeed(s)}>{s}×</button>
          ))}
        </div>
        <input type="range" min={0} max={Math.max(0, total - 1)} value={upToIdx}
          style={{flex:1,height:4,accentColor:'var(--info)',minWidth:60,cursor:'pointer'}}
          onChange={e => onScrub(Number(e.target.value))}
          onMouseUp={e => onScrubEnd(Number(e.target.value))}
          onTouchEnd={e => onScrubEnd(Number(e.target.value))}
        />
        <span style={{fontFamily:'var(--f-mono)',fontSize:'9.5px',color:'var(--fg-3)',flexShrink:0}}>{pct}%</span>
        <select style={selStyle} value={replayDate}
          disabled={datesLoading || !availableDates.length}
          onChange={e => onDateChange(e.target.value, { runId: '' })}>
          {availableDates.slice().reverse().map(d => <option key={d} value={d}>{_fmtDateOption(d, tradeCounts, modelsByDate)}</option>)}
        </select>
        {replayRunId && (
          <span style={{fontFamily:'var(--f-mono)',fontSize:'9px',color:'var(--fg-3)',flexShrink:0}}
            title={replayRunId}>run {replayRunId.slice(0, 8)}…</span>
        )}
      </div>
      <div className="t-status-cells" style={{flexShrink:0}}>
        <div className="t-scell big"><span className="k">P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl,2)}</span></div>
        <div className="t-scell"><span className="k">Trades</span><span className="v">{tradesCount}<span className="sub"> / {winRate}%wr</span></span></div>
        <div className="t-scell"><span className="k">WS</span><span className={`v ${ws==='connected'?'pos':'warn'}`} style={{fontSize:'10px'}}>{ws}</span></div>
      </div>
    </div>
  );
}

function _replayBootParams() {
  try {
    const p = new URLSearchParams(window.location.search);
    return {
      date: String(p.get('date') || p.get('replay_date') || '').trim(),
      runId: String(p.get('run_id') || '').trim(),
    };
  } catch (_) {
    return { date: '', runId: '' };
  }
}

function _syncReplayUrl({ date, runId }) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('mode', 'replay');
    if (date) url.searchParams.set('date', date);
    else url.searchParams.delete('date');
    if (runId) url.searchParams.set('run_id', runId);
    else url.searchParams.delete('run_id');
    window.history.replaceState({}, '', url);
  } catch (_) { /* ignore */ }
}

// ── ReplayMonitorDark ────────────────────────────────────────────────────
function ReplayMonitorDark({ onModeSwitch }) {
  const _boot = _replayBootParams();
  const [session,        setSession]        = _s(null);
  const [upToIdx,        setUpToIdx]        = _s(0);
  const [isPlaying,      setIsPlaying]      = _s(false);
  const [speed,          setSpeed]          = _s(4);
  const [replayDate,     setReplayDate]     = _s(_boot.date || '');
  const [replayRunId,    setReplayRunId]    = _s(_boot.runId || '');
  const [replayError,    setReplayError]    = _s('');
  const [wsStatus,       setWsStatus]       = _s('idle');
  const [availableDates, setAvailableDates] = _s([]);
  const [tradeCounts, setTradeCounts] = _s({});
  const [modelsByDate, setModelsByDate] = _s({});
  const [datesLoading,   setDatesLoading]   = _s(true);
  const [selectedTrade,  setSelectedTrade]  = _s(null);
  const [runtimeConfig,  setRuntimeConfig]  = _s(null);
  const [availableModels,setAvailableModels]= _s([]);
  const [blockerFunnel,  setBlockerFunnel]  = _s(null);
  const [brainData,      setBrainData]      = _s(null);
  const [timeline,       setTimeline]       = _s(null);
  const [tlOutcome,      setTlOutcome]      = _s('blocked');
  const [tlCollapse,     setTlCollapse]     = _s(true);
  const [diagLoading,    setDiagLoading]    = _s(false);
  const [diagError,      setDiagError]      = _s('');
  const wsRef         = _r(null);
  const upToIdxRef    = _r(0);
  const speedRef      = _r(4);
  const replayDateRef = _r(_boot.date || '');
  const replayRunIdRef = _r(_boot.runId || '');
  const fitRef        = _r(null);

  // Diagnostics: load current model + available models, blocker-funnel aggregate
  // for the selected date, AND a per-minute decisions timeline so the operator
  // can scroll snapshot-by-snapshot to see exactly why each minute was blocked.
  // Re-fires whenever replayDate or the timeline outcome filter changes.
  const fetchDiag = _cb(() => {
    const date = replayDateRef.current;
    setDiagLoading(true); setDiagError('');
    const stateP = fetch('/api/strategy/current/state?mode=replay&latest_n=0')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)));
    const funnelP = date
      ? fetch(`/api/strategy/blocker-funnel?mode=replay&date=${encodeURIComponent(date)}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`funnel HTTP ${r.status}`)))
      : Promise.resolve(null);
    const tlOutcomeQ = tlOutcome ? `&outcome=${encodeURIComponent(tlOutcome)}` : '';
    const tlCollapseQ = tlCollapse ? `&collapse=true` : '';
    const tlP = date
      ? fetch(`/api/strategy/decisions?mode=replay&date=${encodeURIComponent(date)}&limit=500${tlOutcomeQ}${tlCollapseQ}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`decisions HTTP ${r.status}`)))
      : Promise.resolve(null);
    const brainP = fetch('/api/strategy/brain/status?mode=replay')
      .then(r => r.ok ? r.json() : Promise.resolve({ available: false, reason: `HTTP ${r.status}` }))
      .catch(() => ({ available: false, reason: 'fetch failed' }));
    Promise.all([stateP, funnelP, tlP, brainP])
      .then(([state, funnel, tl, brain]) => {
        setRuntimeConfig(state?.runtime_config || null);
        setAvailableModels(Array.isArray(state?.available_models) ? state.available_models : []);
        setBlockerFunnel(funnel);
        setTimeline(tl);
        setBrainData(brain);
        setDiagLoading(false);
      })
      .catch(err => { setDiagError(err.message || String(err)); setDiagLoading(false); });
  }, [tlOutcome, tlCollapse]);

  _e(() => { fetchDiag(); /* refetch on date or filter change */ }, [replayDate, tlOutcome, tlCollapse]);

  _e(() => { upToIdxRef.current    = upToIdx;    }, [upToIdx]);
  _e(() => { speedRef.current      = speed;      }, [speed]);
  _e(() => { replayDateRef.current = replayDate; }, [replayDate]);
  _e(() => { replayRunIdRef.current = replayRunId; }, [replayRunId]);

  function openReplaySocket() {
    if (wsRef.current) return;
    wsRef.current = TC.makeMonitorWS(
      () => {
        const sub = {
          action:'subscribe', mode:'replay', date:replayDateRef.current,
          up_to_idx:upToIdxRef.current, playing:false, speed:speedRef.current,
        };
        if (replayRunIdRef.current) sub.run_id = replayRunIdRef.current;
        return sub;
      },
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            setSession(msg.session); setUpToIdx(msg.up_to_idx); setIsPlaying(false);
            setReplayDate(msg.session.date || ''); replayDateRef.current = msg.session.date || '';
            setReplayError('');
          } else if (msg.type === 'tick') {
            setUpToIdx(msg.up_to_idx);
          } else if (msg.type === 'state') {
            setUpToIdx(msg.up_to_idx); setIsPlaying(msg.is_playing); setSpeed(msg.speed);
          } else if (msg.type === 'error') {
            setReplayError(msg.message || 'Replay date load failed.'); setIsPlaying(false);
          }
        },
      }
    );
  }

  _e(() => { return () => { if (wsRef.current) wsRef.current.close(); }; }, []);

  _e(() => {
    let alive = true;
    fetch('/api/historical/replay/dates?limit=250')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(payload => {
        if (!alive) return;
        setAvailableDates(Array.isArray(payload.dates) ? payload.dates : []);
        setTradeCounts(payload.trade_counts || {});
        setModelsByDate(payload.models_by_date || {});
        setDatesLoading(false);
        const boot = _replayBootParams();
        const pick = boot.date || payload.latest;
        if (pick) handleDateChange(pick, { runId: boot.runId || replayRunIdRef.current });
        else setReplayError('No replay dates found in historical snapshots.');
      })
      .catch(err => { if (!alive) return; setDatesLoading(false); setReplayError('Failed to load dates: ' + err.message); });
    return () => { alive = false; };
  }, []);

  function sendControl(patch) { wsRef.current && wsRef.current.send({ action:'control', ...patch }); }
  function handlePlay()    { setIsPlaying(true);  sendControl({ play: true }); }
  function handlePause()   { setIsPlaying(false); sendControl({ play: false }); }
  function handleSpeed(s)  { setSpeed(s);          sendControl({ speed: s }); }
  function handleScrub(idx){ setUpToIdx(idx); }
  function handleScrubEnd(idx) { setUpToIdx(idx); setIsPlaying(false); sendControl({ seek:idx, play:false }); }
  function handleReset()   { setUpToIdx(0); setIsPlaying(false); sendControl({ seek:0, play:false }); }
  function handleDateChange(newDate, opts = {}) {
    if (!newDate) return;
    const nextRunId = opts.runId !== undefined ? String(opts.runId || '').trim() : replayRunIdRef.current;
    setReplayDate(newDate); replayDateRef.current = newDate;
    setReplayRunId(nextRunId); replayRunIdRef.current = nextRunId;
    _syncReplayUrl({ date: newDate, runId: nextRunId });
    setReplayError(''); setIsPlaying(false);
    setSession(null); setUpToIdx(0);
    openReplaySocket();
    const sub = {
      action:'subscribe', mode:'replay', date:newDate, up_to_idx:0, playing:false, speed:speedRef.current,
    };
    if (nextRunId) sub.run_id = nextRunId;
    const sent = wsRef.current && wsRef.current.send(sub);
    if (!sent) setWsStatus('connecting');
  }
  // keyboard nav
  _e(() => {
    const fn = e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      if ((e.key === 'j' || e.key === 'J') && session) {
        const trades = (session.trades||[]).filter(t=>t.exitIdx<=upToIdx).map(_bridgeTrade);
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i > 0) setSelectedTrade(trades[i-1]);
      } else if ((e.key === 'k' || e.key === 'K') && session) {
        const trades = (session.trades||[]).filter(t=>t.exitIdx<=upToIdx).map(_bridgeTrade);
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i >= 0 && i < trades.length-1) setSelectedTrade(trades[i+1]);
      } else if (e.key === ' ' && !e.target.closest('button')) {
        e.preventDefault();
        isPlaying ? handlePause() : handlePlay();
      }
    };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [session, upToIdx, selectedTrade, isPlaying]);

  // Loading state
  if (!session) {
    const selStyle = { fontFamily:'var(--f-mono)', fontSize:'10px', background:'var(--bg-2)',
      color:'var(--fg-1)', border:'1px solid var(--line-2)', borderRadius:'var(--r-2)', padding:'2px 8px' };
    return (
      <div className="cockpit">
        <div className="ticker-bar">
          <div className="t-brand"><span className="t-brand-mark"/><b>QUANT</b><span>OPS</span><span className="ver">replay</span></div>
          <div/><div/>
        </div>
        <div className="t-status-bar">
          <div className="t-mode-toggle">
            <button onClick={() => onModeSwitch('live')}><span className="dot live"/>Live</button>
            <button className="active"><span className="dot replay"/>Replay</button>
            <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
          </div>
          <div style={{display:'flex',alignItems:'center',gap:8,padding:'0 12px'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:'10px',color:'var(--fg-3)'}}>Date</span>
            <select style={selStyle} value={replayDate}
              disabled={datesLoading || !availableDates.length}
              onChange={e => handleDateChange(e.target.value, { runId: '' })}>
              {!replayDate && <option value="">{datesLoading ? 'Loading…' : 'Select date'}</option>}
              {availableDates.slice().reverse().map(d => <option key={d} value={d}>{_fmtDateOption(d, tradeCounts, modelsByDate)}</option>)}
            </select>
          </div>
          <div/>
        </div>
        <div className="t-loading">
          <div style={{textAlign:'center'}}>
            <div style={{color:'var(--fg-2)',marginBottom:8}}>
              {datesLoading          ? 'Loading replay dates…' :
               wsStatus==='connecting' ? 'Connecting to server…' :
               wsStatus==='disconnected' ? 'Reconnecting…' :
               replayError || 'Select a date above to load a session.'}
            </div>
            {replayError && <div style={{fontSize:10,color:'var(--fg-4)'}}>Try a different date or wait for replay data to be generated.</div>}
          </div>
        </div>
        <div className="t-log-strip">
          <span className="t-chip" style={{fontSize:'8.5px'}}>LOG</span>
          <div className="t-log-feed"><span style={{color:'var(--fg-4)'}}>Awaiting session</span></div>
          <div className="t-log-perf"><span><span className="k">ws</span>{wsStatus}</span></div>
        </div>
      </div>
    );
  }

  const candles     = session.candles || [];
  const rawTrades   = (session.trades || []).filter(t => t.exitIdx <= upToIdx);
  const trades      = rawTrades.map(_bridgeTrade);
  const signals     = (session.signals || []).filter(s => s.idx <= upToIdx).slice(-120).reverse();
  const strategies  = _makeStrategies(trades);
  const sessionPnl  = rawTrades.reduce((a,t) => a + (t.pnlPct||0), 0);
  const wins        = rawTrades.filter(t => (t.pnlPct||0) > 0).length;
  const winRate     = rawTrades.length ? Math.round(wins/rawTrades.length*100) : 0;
  const vtLabel     = candles[upToIdx]?.label ||
    new Date(candles[upToIdx]?.t||0).toLocaleTimeString('en-IN',
      { hour:'2-digit', minute:'2-digit', hour12:false, timeZone:'Asia/Kolkata' });
  const displayTrade = selectedTrade || (trades.length > 0 ? trades[0] : null);
  const noData = candles.length > 0 && session.trades.length === 0
    && (session.alerts||[]).some(a => a.level === 'warn');
  const banner = replayError || (noData ? `No high-confidence ML signals on ${replayDate}.` : '');

  return (
    <div className="cockpit">
      <ReplayTickerBar date={session.date} vtLabel={vtLabel}
        instrument={session.instrument} isPlaying={isPlaying}/>
      <ReplayStatusBar
        sessionPnl={sessionPnl} tradesCount={trades.length} winRate={winRate}
        isPlaying={isPlaying} speed={speed} upToIdx={upToIdx} total={candles.length}
        availableDates={availableDates} tradeCounts={tradeCounts} modelsByDate={modelsByDate} replayDate={replayDate}
        replayRunId={replayRunId || session.runId || ''}
        onPlay={handlePlay} onPause={handlePause} onSpeed={handleSpeed}
        onScrub={handleScrub} onScrubEnd={handleScrubEnd} onReset={handleReset}
        onDateChange={handleDateChange} onModeSwitch={onModeSwitch}
        ws={wsStatus} datesLoading={datesLoading}
      />

      {/* The 1fr workspace row — flex column so optional banner doesn't break the grid */}
      <div style={{display:'flex',flexDirection:'column',minHeight:0,overflow:'hidden'}}>
        {banner && (
          <div style={{background:'var(--warn-wash)',borderBottom:'1px solid rgba(245,165,36,0.25)',
            padding:'5px 14px',fontFamily:'var(--f-mono)',fontSize:10.5,color:'var(--warn)',
            display:'flex',alignItems:'center',gap:10,flexShrink:0}}>
            <span style={{flex:1}}>{banner}</span>
          </div>
        )}
        <div className="t-workspace" style={{flex:1,minHeight:0}}>
          <EngineRoster strategies={strategies} dailyRisk={sessionPnl<0?Math.abs(sessionPnl)/2:0} brainData={brainData}/>

          <div className="t-center">
            <div className="t-chart-panel">
              <div className="t-panel-head">
                <div className="t-panel-title">
                  {session.instrument} · 1m
                  <span className="count">{Math.min(upToIdx+1,candles.length)}/{candles.length} bars</span>
                </div>
                <div className="t-panel-actions">
                  <button className="t-btn ghost sm" title="Fit chart"
                    onClick={() => fitRef.current && fitRef.current()}>fit</button>
                  {displayTrade && <span className="t-chip amber">● {displayTrade.id}</span>}
                </div>
              </div>
              <div style={{flex:1,minHeight:0}}>
                <LWChart
                  candles={candles} upToIdx={upToIdx}
                  trades={trades} signals={signals}
                  selectedTrade={displayTrade} selectedSignal={null}
                  onSelectTrade={setSelectedTrade} onSelectSignal={() => {}}
                  height="100%" isPlaying={isPlaying} fitRef={fitRef}
                />
              </div>
            </div>
            <Tape
              session={session} trades={[...trades].reverse()} signals={signals}
              selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
              flashId={null}
              diag={{ runtimeConfig, availableModels, blockerFunnel,
                      timeline, brainData,
                      loading: diagLoading, error: diagError,
                      date: replayDate, onRefresh: fetchDiag,
                      onTimelineFilterChange: setTlOutcome,
                      onTimelineCollapseChange: setTlCollapse,
                      activeRunId: replayRunId || (session && session.runId ? session.runId : null) }}
            />
          </div>

          <div className="t-rail right" style={{display:'flex',flexDirection:'column'}}>
            <div className="t-panel-head">
              <div className="t-panel-title">Trade Inspector</div>
              <div className="t-panel-actions">
                <button className="t-btn ghost sm" title="Prev (K)"
                  onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i>0)setSelectedTrade(trades[i-1]); }}>K↑</button>
                <button className="t-btn ghost sm" title="Next (J)"
                  onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i<trades.length-1)setSelectedTrade(trades[i+1]); }}>↓J</button>
              </div>
            </div>
            <div style={{flex:1,overflowY:'auto',minHeight:0}}>
              <TradeInspector session={session} trade={displayTrade}/>
            </div>
          </div>
        </div>
      </div>

      <LogStrip session={session} alerts={session.alerts} wsStatus={wsStatus}/>
    </div>
  );
}

Object.assign(window, { LiveMonitorDark, ReplayMonitorDark });
